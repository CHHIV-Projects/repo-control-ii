from __future__ import annotations

from datetime import UTC, datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import time
from typing import Any, Callable, ContextManager, TYPE_CHECKING
import uuid

from ..scanner.git_ops import ScanError, _run_git_bounded, get_branch, get_head_commit
from . import measurements
from .provider import GraphifyProvider, IntelligenceError, MAX_GRAPH_BYTES, Provider, VERSION

if TYPE_CHECKING:
    from ..web.repository_registry import RepositoryEntry

OPERATIONS = ("stats", "lookup", "neighbors", "callers", "callees", "imports", "references", "impact", "tests", "context", "path")
RELATIONS = (
    "calls", "contains", "references", "imports", "method", "imports_from", "uses",
    "indirect_call", "inherits", "rationale_for", "re_exports", "implements", "extends",
)
SOURCE_SUFFIXES = frozenset((".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".cs", ".rb", ".sh"))
EXCLUDED_PARTS = frozenset((".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", "vendor"))
OPTIONS = {"code_only": True, "max_workers": 2, "scope_version": 1, "excluded_parts": sorted(EXCLUDED_PARTS),
           "suffixes": sorted(SOURCE_SUFFIXES), "max_files": 10000, "max_source_bytes": 64 * 1024 * 1024}
DEFAULT_BOUNDS = {"max_bytes": 48 * 1024, "max_nodes": 60, "max_edges": 120, "depth": 2, "timeout": 5}
MAX_BOUNDS = {"max_bytes": 64 * 1024, "max_nodes": 120, "max_edges": 240, "depth": 4, "timeout": 10}
REFRESH_ACTORS = frozenset({"po_lifecycle", "po_manual", "coder"})
QUERY_PROPERTIES = {
    "operation": {"type": "string", "enum": list(OPERATIONS), "default": "stats"},
    "query": {"type": "string", "maxLength": 300, "description": "Node ID, symbol, repository-relative file::symbol, or lexical concept."},
    "target": {"type": "string", "maxLength": 300, "description": "Second endpoint for generic relationship path."},
    "relations": {"type": "array", "items": {"type": "string", "enum": list(RELATIONS)}, "maxItems": len(RELATIONS)},
    "direction": {"type": "string", "enum": ["incoming", "outgoing", "both"]},
    **{key: {"type": "integer", "minimum": 4096 if key == "max_bytes" else 1,
             "maximum": MAX_BOUNDS[key], "default": value} for key, value in DEFAULT_BOUNDS.items()},
}


def encoded(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")


def safe_path(root: Path, relative: str) -> Path:
    path = PurePosixPath(relative)
    if not relative or path.is_absolute() or "\\" in relative or any(p in {"", ".", ".."} for p in relative.split("/")):
        raise IntelligenceError("Unsafe repository/artifact relative path.")
    current = root
    for part in path.parts:
        current /= part
        if current.is_symlink():
            raise IntelligenceError("Symlinked intelligence/source path is not permitted.")
    return current


def artifact_root(repository: RepositoryEntry, state_root: Path) -> Path:
    # Scanner publication replaces <state_root>/<repository_id>; keep this namespace independent.
    root = state_root.expanduser().absolute() / "intelligence" / repository.repository_id
    if root.resolve().is_relative_to(repository.path.resolve()):
        raise IntelligenceError("Intelligence state must be external to repository content.")
    for parent in (root, *root.parents):
        if parent.is_symlink():
            raise IntelligenceError("Symlinked intelligence state root is not permitted.")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root.chmod(0o700)
    return root


def source_snapshot(repository: RepositoryEntry, destination: Path | None = None) -> dict[str, Any]:
    raw, errors, code, truncated = _run_git_bounded(
        repository.path, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        stdout_limit=2 * 1024 * 1024,
    )
    if code or truncated:
        raise IntelligenceError(f"Cannot enumerate bounded source scope: {errors.decode(errors='replace')[:500]}")
    files = {}
    total = 0
    for relative in sorted({os.fsdecode(item) for item in raw.split(b"\0") if item}):
        path = PurePosixPath(relative)
        if path.suffix.lower() not in SOURCE_SUFFIXES or any(part in EXCLUDED_PARTS for part in path.parts):
            continue
        source = safe_path(repository.path, relative)
        try:
            descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            continue  # A tracked deletion is represented by absence from this content identity.
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise IntelligenceError("Only regular source files are supported.")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read(2 * 1024 * 1024 + 1)
        finally:
            os.close(descriptor)
        total += len(content)
        if len(content) > 2 * 1024 * 1024 or total > OPTIONS["max_source_bytes"] or len(files) >= OPTIONS["max_files"]:
            raise IntelligenceError("Source snapshot exceeds file/count/byte safety bounds.")
        files[relative] = hashlib.sha256(content).hexdigest()
        if destination is not None:
            target = safe_path(destination, relative)
            target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            target.write_bytes(content)
    return {
        "content_hash": hashlib.sha256(encoded(files)).hexdigest(),
        "files": files, "source_bytes": total, "options": OPTIONS,
        "head": get_head_commit(repository.path), "branch": get_branch(repository.path),
    }


def write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(encoded(value) + b"\n")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_manifest(root: Path) -> dict[str, Any] | None:
    path = safe_path(root, "manifest.json")
    if not path.exists():
        return None
    if path.stat().st_size > 2 * 1024 * 1024:
        raise IntelligenceError("Intelligence manifest exceeds safety bound.")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise IntelligenceError("Malformed intelligence manifest.")
    return value


def graph_status(repository: RepositoryEntry, root: Path) -> dict[str, Any]:
    manifest = read_manifest(root)
    if manifest is None:
        return {"state": "MISSING", "generation": None}
    view = {key: manifest.get(key) for key in ("state", "generation", "built_at", "producer", "graph_schema", "graph_hash", "error")}
    if manifest.get("state") == "BUILDING":
        with safe_path(root, "build.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return view
        view.update(state="FAILED", error="Build was interrupted; explicit rebuild required.")
        return view
    if manifest.get("state") == "FAILED":
        return view
    if (manifest.get("producer") != VERSION or manifest.get("graph_schema") != 1
        or manifest.get("repository_id") != repository.repository_id
        or manifest.get("source", {}).get("options") != OPTIONS):
        view["state"] = "INCOMPATIBLE"
        return view
    generation = manifest.get("generation", "")
    if not isinstance(generation, str) or len(generation) != 32 or any(c not in "0123456789abcdef" for c in generation):
        raise IntelligenceError("Invalid graph generation identity.")
    graph = safe_path(root, f"generations/{generation}/graph.json")
    if not graph.is_file() or graph.stat().st_size > MAX_GRAPH_BYTES:
        view.update(state="FAILED", error="Graph is missing or oversized.")
        return view
    if hashlib.sha256(graph.read_bytes()).hexdigest() != manifest.get("graph_hash"):
        view.update(state="FAILED", error="Graph hash mismatch.")
        return view
    current = source_snapshot(repository)
    previous = manifest["source"]
    view["source"] = {key: previous.get(key) for key in ("content_hash", "head", "branch", "source_bytes")}
    view["source"]["file_count"] = len(previous["files"])
    view["state"] = "CURRENT" if all(current[key] == previous.get(key) for key in ("content_hash", "head", "branch", "options")) else "STALE"
    return view


class RefreshAuthorizationError(IntelligenceError):
    pass


def build_graph(
    repository: RepositoryEntry,
    state_root: Path,
    provider: Provider | None = None,
    *,
    publication_guard: Callable[[], ContextManager[Any]] | None = None,
) -> dict[str, Any]:
    root = artifact_root(repository, state_root)
    provider = provider or GraphifyProvider()
    with safe_path(root, "build.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise IntelligenceError("An intelligence build is already running.") from exc
        previous = read_manifest(root)
        retained = previous.get("generation") if previous else None
        generations = safe_path(root, "generations")
        if generations.exists():
            for candidate in generations.iterdir():
                if (candidate.name == retained or len(candidate.name) != 32
                    or any(c not in "0123456789abcdef" for c in candidate.name)):
                    continue
                if candidate.is_symlink() or not candidate.is_dir():
                    raise IntelligenceError("Unsafe graph generation entry.")
                shutil.rmtree(candidate)
        generation = uuid.uuid4().hex
        output = safe_path(root, f"generations/{generation}")
        output.mkdir(mode=0o700, parents=True)
        source = output / "source"
        source.mkdir(mode=0o700)
        manifest = {
            "state": "BUILDING", "repository_id": repository.repository_id, "repository_key": repository.key,
            "generation": generation, "built_at": datetime.now(UTC).isoformat(),
            "producer": provider.version, "graph_schema": 1,
        }
        write_json(root / "manifest.json", manifest)
        try:
            snapshot = source_snapshot(repository, source)
            if not snapshot["files"]:
                raise IntelligenceError("No supported code files in the selected source scope.")
            provider.build(source, output)
            graph = safe_path(output, "graph.json")
            if graph.stat().st_size > MAX_GRAPH_BYTES:
                raise IntelligenceError("Graph exceeds size cap.")
            payload = json.loads(graph.read_bytes())
            metadata = payload.get("graph", {})
            if metadata.get("schema_version") != 1 or metadata.get("graphify_version") != VERSION:
                raise IntelligenceError("Graph producer/schema incompatible.")
            # Compare after extraction so concurrent editing cannot create a false CURRENT generation.
            after = source_snapshot(repository)
            current = all(snapshot[key] == after[key] for key in ("content_hash", "head", "branch", "options"))
            manifest.update(
                state="CURRENT" if current else "STALE", source=snapshot,
                graph_hash=hashlib.sha256(graph.read_bytes()).hexdigest(),
                stats={"nodes": len(payload["nodes"]), "relationships": len(payload.get("links", payload.get("edges", [])))},
            )
            if current and publication_guard is not None:
                with publication_guard():
                    write_json(root / "manifest.json", manifest)
            else:
                write_json(root / "manifest.json", manifest)
        except (IntelligenceError, ScanError, OSError, ValueError, KeyError) as exc:
            manifest.update(state="FAILED", error=str(exc)[:1500])
            write_json(root / "manifest.json", manifest)
            raise IntelligenceError(f"Intelligence build failed: {exc}") from exc
        finally:
            shutil.rmtree(source)
        return graph_status(repository, root)


def refresh_graph(
    repository: RepositoryEntry,
    state_root: Path,
    *,
    actor: str,
    milestone_id: str | None = None,
    registry_generation: int | None = None,
    publication_guard: Callable[[], ContextManager[Any]] | None = None,
    provider: Provider | None = None,
) -> dict[str, Any]:
    if actor not in REFRESH_ACTORS:
        raise IntelligenceError("Unsupported intelligence refresh actor.")
    if actor in {"po_lifecycle", "coder"} and (
        not isinstance(milestone_id, str) or not milestone_id or len(milestone_id) > 64
    ):
        raise IntelligenceError("A selected milestone is required for this intelligence refresh.")
    if registry_generation is not None and (
        isinstance(registry_generation, bool)
        or not isinstance(registry_generation, int)
        or registry_generation < 0
    ):
        raise IntelligenceError("Invalid repository registry generation for intelligence refresh.")

    started = time.monotonic()
    root = artifact_root(repository, state_root)
    initial = graph_status(repository, root)
    receipt: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "timestamp": datetime.now(UTC).isoformat(),
        "repository_key": repository.key,
        "repository_id": repository.repository_id,
        "actor": actor,
        "registry_generation": registry_generation,
        "milestone_id": milestone_id,
        "triggering_freshness": initial["state"],
        "action": "checking",
        "generation_before": initial.get("generation"),
        "generation_after": initial.get("generation"),
        "source_identity_before": initial.get("source", {}).get("content_hash"),
        "source_identity_after": initial.get("source", {}).get("content_hash"),
        "final_freshness": initial["state"],
        "elapsed_ms": 0.0,
        "error_code": None,
        "completed": False,
    }
    measurements.begin_refresh(root, receipt)

    def finish(
        outcome: str,
        *,
        action: str,
        graph: dict[str, Any],
        error_code: str | None = None,
        error: str | None = None,
        source_identity_before: str | None = None,
    ) -> dict[str, Any]:
        receipt.update(
            action=action,
            generation_after=graph.get("generation"),
            source_identity_before=(
                source_identity_before
                if source_identity_before is not None
                else receipt["source_identity_before"]
            ),
            source_identity_after=graph.get("source", {}).get("content_hash"),
            final_freshness=graph["state"],
            elapsed_ms=(time.monotonic() - started) * 1000,
            error_code=error_code,
            completed=outcome != "FAILED",
        )
        measurements.finish_refresh(root, receipt)
        return {"outcome": outcome, "graph": graph, "receipt": dict(receipt), "error": error}

    if initial["state"] == "CURRENT":
        return finish("CURRENT", action="noop_current", graph=initial)

    try:
        snapshot = source_snapshot(repository)
    except (IntelligenceError, ScanError, OSError, ValueError) as exc:
        return finish(
            "FAILED",
            action="source_check_failed",
            graph=initial,
            error_code="source_check_failed",
            error=str(exc)[:1200],
        )
    if not snapshot["files"]:
        unavailable = {
            "state": "NOT_AVAILABLE",
            "generation": initial.get("generation"),
            "source": {"content_hash": snapshot["content_hash"]},
        }
        return finish(
            "NOT_AVAILABLE",
            action="not_available",
            graph=unavailable,
            source_identity_before=snapshot["content_hash"],
        )

    measurements.finish_refresh(root, {
        **receipt,
        "action": "build",
        "source_identity_before": snapshot["content_hash"],
    })
    try:
        refreshed = build_graph(
            repository,
            state_root,
            provider,
            publication_guard=publication_guard,
        )
        if (
            refreshed["state"] != "CURRENT"
            or refreshed.get("source", {}).get("content_hash") != snapshot["content_hash"]
        ):
            reason = (
                "Source identity changed during the graph build."
                if refreshed["state"] == "STALE"
                else f"Graph refresh did not produce CURRENT evidence ({refreshed['state']})."
            )
            return finish(
                "FAILED",
                action="build",
                graph=refreshed,
                error_code="source_changed" if refreshed["state"] == "STALE" else "non_current",
                error=reason,
                source_identity_before=snapshot["content_hash"],
            )
    except (IntelligenceError, ScanError, OSError, ValueError, KeyError) as exc:
        try:
            final_status = graph_status(repository, root)
        except (IntelligenceError, ScanError, OSError, ValueError, KeyError):
            final_status = {"state": "FAILED", "generation": None}
        cause: BaseException | None = exc
        authorization_changed = False
        while cause is not None:
            if isinstance(cause, RefreshAuthorizationError):
                authorization_changed = True
                break
            cause = cause.__cause__
        return finish(
            "FAILED",
            action="build",
            graph=final_status,
            error_code="authorization_changed" if authorization_changed else "build_failed",
            error=str(exc)[:1200],
            source_identity_before=snapshot["content_hash"],
        )
    return finish(
        "REFRESHED",
        action="build",
        graph=refreshed,
        source_identity_before=snapshot["content_hash"],
    )


def normalize(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or set(arguments) - {
        "operation", "query", "target", "relations", "direction", *DEFAULT_BOUNDS,
    }:
        raise IntelligenceError("Unknown intelligence arguments; arbitrary tools/paths are not supported.")
    operation = arguments.get("operation", "stats")
    if not isinstance(operation, str) or operation not in OPERATIONS:
        raise IntelligenceError("Unsupported intelligence operation.")
    result = {"operation": operation}
    for key, default in DEFAULT_BOUNDS.items():
        value = arguments.get(key, default)
        minimum = 4096 if key == "max_bytes" else 1
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= MAX_BOUNDS[key]:
            raise IntelligenceError(f"{key} must be an integer from {minimum} to {MAX_BOUNDS[key]}.")
        result[key] = value
    for key in ("query", "target"):
        value = arguments.get(key, "")
        if not isinstance(value, str) or len(value) > 300 or any(ord(c) < 32 for c in value):
            raise IntelligenceError(f"{key} must be text of at most 300 characters.")
        result[key] = value.strip()
    if operation != "stats" and not result["query"] or operation == "path" and not result["target"]:
        raise IntelligenceError("This operation requires query (and target for path).")
    if operation == "path" and result["max_nodes"] < 2:
        raise IntelligenceError("Path requires capacity for at least two endpoint nodes.")
    direction = arguments.get("direction", "both")
    if not isinstance(direction, str) or direction not in {"incoming", "outgoing", "both"}:
        raise IntelligenceError("Invalid relationship direction.")
    relations = arguments.get("relations", [])
    if not isinstance(relations, list) or len(relations) > len(RELATIONS) or any(not isinstance(r, str) or r not in RELATIONS for r in relations):
        raise IntelligenceError("Unsupported relationship filter.")
    result.update(direction=direction, relations=sorted(set(relations)))
    if operation in {"callers", "callees"}:
        result.update(direction="incoming" if operation == "callers" else "outgoing", relations=["calls"])
    elif operation == "imports":
        result["relations"] = ["imports", "imports_from", "re_exports"]
    elif operation == "references":
        result["relations"] = ["references", "uses"]
    elif operation in {"impact", "tests"} and not relations:
        result["relations"] = ["calls", "imports", "imports_from", "references", "uses"]
    return result


def bound_envelope(envelope: dict[str, Any], maximum: int) -> None:
    evidence = envelope["evidence"]
    for key in ("nodes", "relationships"):
        evidence.setdefault(key, [])
    evidence.setdefault("omitted_nodes", 0)
    evidence.setdefault("omitted_relationships", 0)
    for row in evidence["nodes"] + evidence["relationships"] + evidence.get("impact", []):
        path = row.get("source_file")
        if path:
            safe_path(Path("/"), path)
    while len(encoded(envelope)) > maximum:
        if evidence.get("native_text"):
            evidence["native_text_bytes_omitted"] = len(evidence["native_text"].encode("utf-8"))
            del evidence["native_text"]
            evidence["native_text_omitted"] = True
        elif evidence.get("impact"):
            evidence["impact"].pop()
            evidence["omitted_impact"] = evidence.get("omitted_impact", 0) + 1
        elif evidence["relationships"]:
            evidence["relationships"].pop()
            evidence["omitted_relationships"] += 1
        elif evidence["nodes"]:
            removed = evidence["nodes"].pop()["id"]
            evidence["omitted_nodes"] += 1
            previous = len(evidence["relationships"])
            evidence["relationships"] = [
                edge for edge in evidence["relationships"] if removed not in {edge["source"], edge["target"]}
            ]
            evidence["omitted_relationships"] += previous - len(evidence["relationships"])
        else:
            raise IntelligenceError("Intelligence envelope metadata exceeds byte bound.")
        envelope["truncated"] = True
        if envelope["outcome"] == "OK":
            envelope["outcome"] = "TRUNCATED"


def intelligence_query(
    repository: RepositoryEntry, state_root: Path, arguments: dict[str, Any], *,
    consumer: str, registry_generation: int, provider: Provider | None = None,
) -> dict[str, Any]:
    started = time.monotonic()
    arguments = normalize(arguments)
    if consumer not in {"Architect", "Coder"}:
        raise IntelligenceError("Invalid intelligence consumer.")
    root = artifact_root(repository, state_root)
    result = {
        "contract_version": "1", "use_id": uuid.uuid4().hex,
        "repository": {"key": repository.key, "repository_id": repository.repository_id, "registry_generation": registry_generation},
        "operation": arguments["operation"], "arguments": arguments,
        "graph": {"state": "FAILED", "generation": None}, "outcome": "FAILED",
        "truncated": False, "warnings": ["Static structural evidence; absence is not proof of no runtime relationship."],
        "evidence": {"nodes": [], "relationships": []},
    }
    try:
        status = graph_status(repository, root)
        result["graph"] = status
        if arguments["operation"] == "stats":
            result["outcome"] = {"CURRENT": "OK", "STALE": "STALE", "FAILED": "FAILED"}.get(status["state"], "INCONCLUSIVE")
            if status["state"] == "CURRENT":
                result["evidence"]["stats"] = read_manifest(root)["stats"]
        elif status["state"] != "CURRENT":
            result["outcome"] = {"STALE": "STALE", "FAILED": "FAILED"}.get(status["state"], "INCONCLUSIVE")
        else:
            remaining = arguments["timeout"] - (time.monotonic() - started)
            if remaining <= 0:
                raise IntelligenceError("Source validation exceeded the request time bound.")
            graph = safe_path(root, f"generations/{status['generation']}/graph.json")
            evidence = (provider or GraphifyProvider()).query(graph, arguments, remaining)
            # Recheck content and generation after native execution; never attribute concurrent edits to CURRENT.
            after = graph_status(repository, root)
            if after["state"] != "CURRENT" or after["generation"] != status["generation"]:
                result.update(graph=after, outcome="STALE")
            else:
                result.update(outcome=evidence.pop("outcome"), evidence=evidence)
                for key, bound in (("nodes", "max_nodes"), ("relationships", "max_edges")):
                    excess = max(0, len(evidence[key]) - arguments[bound])
                    evidence[key] = evidence[key][:arguments[bound]]
                    omitted = "omitted_nodes" if key == "nodes" else "omitted_relationships"
                    evidence[omitted] = evidence.get(omitted, 0) + excess
                visible = {node["id"] for node in evidence["nodes"]}
                edges = evidence["relationships"]
                evidence["relationships"] = [edge for edge in edges if edge["source"] in visible and edge["target"] in visible]
                evidence["omitted_relationships"] += len(edges) - len(evidence["relationships"])
                result["truncated"] = bool(
                    evidence.get("omitted_nodes") or evidence.get("omitted_relationships")
                    or evidence.get("omitted_impact") or result["outcome"] == "TRUNCATED"
                )
                if result["truncated"] and result["outcome"] == "OK":
                    result["outcome"] = "TRUNCATED"
        if time.monotonic() - started > arguments["timeout"]:
            raise IntelligenceError("Intelligence request exceeded its time bound.")
        bound_envelope(result, arguments["max_bytes"])
    except (IntelligenceError, ScanError, OSError, ValueError, KeyError, TypeError) as exc:
        result.update(outcome="FAILED", evidence={"nodes": [], "relationships": []}, error=str(exc)[:1200], truncated=False)
        bound_envelope(result, arguments["max_bytes"])
    result["counts"] = {
        "nodes": len(result["evidence"]["nodes"]), "relationships": len(result["evidence"]["relationships"]),
        "omitted_nodes": result["evidence"].get("omitted_nodes", 0),
        "omitted_relationships": result["evidence"].get("omitted_relationships", 0),
        "impact_candidates": len(result["evidence"].get("impact", [])),
        "omitted_impact": result["evidence"].get("omitted_impact", 0),
    }
    # Account for count metadata without changing the established response budget.
    bound_envelope(result, arguments["max_bytes"] - 256)
    result["counts"].update(
        nodes=len(result["evidence"]["nodes"]), relationships=len(result["evidence"]["relationships"]),
        omitted_nodes=result["evidence"].get("omitted_nodes", 0),
        omitted_relationships=result["evidence"].get("omitted_relationships", 0),
        impact_candidates=len(result["evidence"].get("impact", [])),
        omitted_impact=result["evidence"].get("omitted_impact", 0),
    )
    evidence_metrics = measurements.measure_text(
        encoded(result).decode("ascii"), surface=measurements.NORMAL_EVIDENCE_SURFACE,
    )
    measurements.record(root, {
        "id": result["use_id"], "timestamp": datetime.now(UTC).isoformat(), "repository_key": repository.key,
        "generation": result["graph"].get("generation"), "consumer": consumer, "operation": arguments["operation"],
        "latency_ms": (time.monotonic() - started) * 1000,
        "response_bytes": evidence_metrics["evidence_bytes"],
        "evidence_characters": evidence_metrics["evidence_characters"],
        "estimated_evidence_tokens": evidence_metrics["estimated_evidence_tokens"],
        "evidence_metric_method": evidence_metrics["evidence_metric_method"],
        "evidence_metric_version": evidence_metrics["evidence_metric_version"],
        "evidence_metric_surface": evidence_metrics["evidence_metric_surface"],
        "evidence_metric_quality": evidence_metrics["evidence_metric_quality"],
        "nodes": result["counts"]["nodes"], "relationships": result["counts"]["relationships"],
        "outcome": result["outcome"], "truncated": result["truncated"],
    })
    return result
