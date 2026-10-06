from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..analysis.manager import _verify_existing_analysis
from ..compare.manager import _verify_existing_comparison
from ..context.policy import MATCH_STATUS_MATCHED
from ..scanner.util import encode_path_order
from ..snapshot.manager import _verify_existing_snapshot


@dataclass(frozen=True)
class DashboardView:
    repository_name: str
    repository_root: str
    branch: str
    head: str
    head_short: str
    head_subject: str
    head_tags: list[str]
    workflow_state: str
    staged_paths: list[str]
    unstaged_paths: list[str]
    untracked_paths: list[str]
    unmerged_paths: list[str]
    staged_count: int
    unstaged_count: int
    untracked_count: int
    unmerged_count: int
    git_operation: str
    upstream_ref: str
    upstream_relation: str
    upstream_ahead: str
    upstream_behind: str
    current_snapshot_candidate_id: str
    matching_snapshot_id: str
    remote_refresh_note: str


@dataclass(frozen=True)
class ContextView:
    context_id: str
    repository_id: str
    repository_root: str
    branch: str
    head_commit: str
    head_short: str
    working_tree_origin: str
    freshness_status: str
    freshness_detail: str
    query: str
    canonical_query: str
    match_status: str
    seed_count: int
    selected_files: list[dict[str, Any]]
    selected_symbols: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    tests: list[dict[str, Any]]
    grouped_tests: list[dict[str, Any]]
    limitations: list[dict[str, Any]]
    parse_limitations: list[dict[str, Any]]
    selection_metadata: dict[str, Any]


@dataclass(frozen=True)
class SnapshotSummary:
    snapshot_id: str
    head_commit: str
    head_short: str
    branch: str
    working_tree_clean: bool
    current_state_match: bool
    summary_label: str
    classification: str
    artifact_mtime_utc: str
    staged_count: int
    unstaged_count: int
    untracked_count: int
    unmerged_count: int


@dataclass(frozen=True)
class ComparisonSummary:
    comparison_id: str
    before_snapshot_id: str
    after_snapshot_id: str
    before_label: str
    after_label: str
    aggregate_counts: dict[str, Any]
    artifact_mtime_utc: str


@dataclass(frozen=True)
class AnalysisSummary:
    analysis_id: str
    comparison_id: str
    model_name: str
    provider: str
    summary: str
    summary_evidence_ids: list[str]
    review_signals: list[dict[str, Any]]
    questions_for_human_review: list[dict[str, Any]]


@dataclass(frozen=True)
class WorkflowExecutionSummary:
    execution_id: str
    status: str
    result: str
    artifact_mtime_utc: str
    head_after: str
    head_after_short: str
    head_after_subject: str
    head_after_tags: list[str]


@dataclass(frozen=True)
class WorkflowPlanSummary:
    plan_id: str
    kind: str
    action: str
    branch: str
    head: str
    head_short: str
    head_subject: str
    head_tags: list[str]
    count: int
    scope_paths: list[str]
    artifact_mtime_utc: str
    compatibility: str


@dataclass(frozen=True)
class WorkflowArtifactSummary:
    family: str
    plan: WorkflowPlanSummary
    execution: WorkflowExecutionSummary | None
    chronology_time_utc: str
    chronology_epoch: float


def _short_sha(value: str) -> str:
    return value[:7] if len(value) >= 7 else value


def _run_git_subject(repository_root: Path, head: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repository_root), "show", "-s", "--format=%s", head],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return "(commit subject unavailable)"
    subject = proc.stdout.decode("utf-8", errors="replace").strip()
    return subject or "(empty subject)"


def _run_git_tags_at_head(repository_root: Path, head: str) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(repository_root), "tag", "--points-at", head],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return []
    tags = [line.strip() for line in proc.stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return sorted(tags, key=lambda item: item.encode("utf-8"))


def _working_tree_detail_summary(working_tree: dict[str, Any]) -> str:
    staged = int(working_tree.get("staged", {}).get("count", 0))
    unstaged = int(working_tree.get("unstaged", {}).get("count", 0))
    untracked = int(working_tree.get("untracked", {}).get("count", 0))
    conflicts = int(working_tree.get("unmerged", {}).get("count", 0))
    return f"staged={staged}, unstaged={unstaged}, untracked={untracked}, conflicts={conflicts}"


def _working_tree_origin_summary(working_tree: dict[str, Any]) -> str:
    clean = bool(working_tree.get("is_clean", False))
    entries = working_tree.get("entries")
    if not isinstance(entries, list):
        return "unknown"
    tracked = sum(1 for item in entries if isinstance(item, dict) and item.get("kind") == "tracked")
    untracked = sum(1 for item in entries if isinstance(item, dict) and item.get("kind") == "untracked")
    unmerged = sum(1 for item in entries if isinstance(item, dict) and item.get("kind") == "unmerged")
    return f"clean={clean}, tracked_entries={tracked}, untracked_entries={untracked}, unmerged_entries={unmerged}"


def _normalize_worktree_entries(entries: Any) -> tuple[str, tuple[tuple[str, str, str], ...]] | None:
    if not isinstance(entries, list):
        return None
    normalized: list[tuple[str, str, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            return None
        kind = item.get("kind")
        path = item.get("path")
        if not isinstance(kind, str) or not isinstance(path, str):
            return None
        xy = item.get("xy")
        xy_text = xy if isinstance(xy, str) else ""
        normalized.append((kind, path, xy_text))
    normalized.sort(key=lambda row: (encode_path_order(row[1]), row[0], row[2].encode("utf-8")))
    return ("ok", tuple(normalized))


def _context_freshness(
    *,
    context_payload: dict[str, Any],
    current_git_state: dict[str, Any],
    current_repository_id: str,
    current_repository_root: Path,
) -> tuple[str, str]:
    context_repository_id = context_payload.get("repository_id")
    context_repository_root = context_payload.get("repository_root")
    if not isinstance(context_repository_id, str) or not isinstance(context_repository_root, str):
        return ("UNKNOWN", "Required repository identity is unavailable in context artifact.")

    if context_repository_id != current_repository_id or context_repository_root != str(current_repository_root):
        return ("STALE", "Repository identity/root differs from current bound repository.")

    context_branch = context_payload.get("branch")
    current_branch = current_git_state.get("branch")
    if not isinstance(context_branch, dict) or not isinstance(current_branch, dict):
        return ("UNKNOWN", "Required branch identity is unavailable for safe comparison.")
    if context_branch.get("state") != current_branch.get("state") or context_branch.get("name") != current_branch.get("name"):
        return ("STALE", "Branch at context generation differs from current branch.")

    context_head = context_payload.get("head_commit")
    current_head = current_git_state.get("head")
    if not isinstance(context_head, str) or not isinstance(current_head, str):
        return ("UNKNOWN", "Required HEAD value is unavailable for safe comparison.")
    if context_head != current_head:
        return ("STALE", "HEAD differs from context generation state.")

    context_worktree = context_payload.get("working_tree")
    current_worktree = current_git_state.get("working_tree")
    if not isinstance(context_worktree, dict) or not isinstance(current_worktree, dict):
        return ("UNKNOWN", "Working-tree origin/current facts are unavailable for safe comparison.")

    context_clean = context_worktree.get("is_clean")
    current_clean = current_worktree.get("is_clean")
    if not isinstance(context_clean, bool) or not isinstance(current_clean, bool):
        return ("UNKNOWN", "Working-tree cleanliness field is unavailable for safe comparison.")

    context_entries = _normalize_worktree_entries(context_worktree.get("entries"))
    current_entries = _normalize_worktree_entries(current_worktree.get("entries"))
    if context_entries is None or current_entries is None:
        return ("UNKNOWN", "Working-tree entries are unavailable or not safely comparable.")

    if context_clean != current_clean or context_entries[1] != current_entries[1]:
        return ("STALE", "Working-tree state differs from context generation state.")

    return ("CURRENT", "Repository, branch, HEAD, and working-tree state all match context origin.")


def _group_related_tests(tests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in tests:
        test_info = row.get("test_info") or {}
        test_file = test_info.get("test_file") if isinstance(test_info.get("test_file"), str) else "unknown"
        test_name = test_info.get("test_name") if isinstance(test_info.get("test_name"), str) else "unknown"
        target_file = row.get("target_file") if isinstance(row.get("target_file"), str) else "unknown"
        target_symbol = row.get("target_symbol") if isinstance(row.get("target_symbol"), str) else "unknown"
        grouped[(test_file, test_name, target_file, target_symbol)].append(row)

    rows: list[dict[str, Any]] = []
    for key in sorted(grouped.keys(), key=lambda item: (encode_path_order(item[0]), item[1].encode("utf-8"), encode_path_order(item[2]), item[3].encode("utf-8"))):
        entries = sorted(
            grouped[key],
            key=lambda item: (
                (item.get("test_info") or {}).get("reference_kind") or "",
                item.get("source_line") if isinstance(item.get("source_line"), int) else 2**31 - 1,
                (item.get("test_info") or {}).get("test_class") or "",
                item.get("target_symbol_kind") or "",
            ),
        )
        details = []
        for item in entries:
            test_info = item.get("test_info") or {}
            details.append(
                {
                    "reference_kind": test_info.get("reference_kind") or "unknown",
                    "source_line": item.get("source_line"),
                    "test_class": test_info.get("test_class") or "(top-level)",
                    "target_symbol_kind": item.get("target_symbol_kind") or "unknown",
                    "source_symbol_kind": item.get("source_symbol_kind") or "unknown",
                }
            )

        rows.append(
            {
                "test_file": key[0],
                "test_name": key[1],
                "target_file": key[2],
                "target_symbol": key[3],
                "evidence_count": len(entries),
                "details": details,
            }
        )
    return rows


def _snapshot_label(branch: str, head: str, clean: bool, current_match: bool) -> str:
    state = "clean" if clean else "dirty"
    match = "matches current" if current_match else "historical"
    return f"{branch} @ {_short_sha(head)} - {state}, {match}"


def _comparison_state_label(state_payload: dict[str, Any]) -> str:
    branch = state_payload.get("branch", {})
    branch_name = branch.get("name") if branch.get("state") == "attached" else "(detached)"
    head = state_payload.get("head_commit") or "unknown"
    wt = state_payload.get("working_tree") or {}
    clean = bool(wt.get("is_clean", False))
    return f"{branch_name} @ {_short_sha(head)} - {'clean' if clean else 'dirty'}"


def _sorted_ids(path: Path) -> list[str]:
    if not path.exists() or not path.is_dir():
        return []
    ids = [child.name for child in path.iterdir() if child.is_dir()]
    return sorted(ids, key=lambda item: item.encode("utf-8"))


def _sorted_ids_by_mtime(path: Path) -> list[str]:
    if not path.exists() or not path.is_dir():
        return []
    rows: list[tuple[float, str]] = []
    for child in path.iterdir():
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            continue
        rows.append((mtime, child.name))
    rows.sort(key=lambda row: (-row[0], row[1].encode("utf-8")))
    return [name for _mtime, name in rows]


def _snapshot_mtime_utc(snapshot_dir: Path) -> str:
    try:
        mtime = snapshot_dir.stat().st_mtime
    except OSError:
        return "unknown"
    return datetime.fromtimestamp(mtime, UTC).isoformat().replace("+00:00", "Z")


def _snapshot_working_tree_counts(snapshot_payload: dict[str, Any]) -> tuple[int, int, int, int]:
    working_tree = snapshot_payload.get("working_tree")
    if not isinstance(working_tree, dict):
        return (0, 0, 0, 0)
    entries = working_tree.get("entries")
    if not isinstance(entries, list):
        return (0, 0, 0, 0)

    staged_paths: set[str] = set()
    unstaged_paths: set[str] = set()
    untracked_paths: set[str] = set()
    unmerged_paths: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        if not isinstance(path, str):
            continue

        kind = entry.get("kind")
        if kind == "untracked":
            untracked_paths.add(path)
            continue
        if kind == "unmerged":
            unmerged_paths.add(path)
            continue

        xy = entry.get("xy")
        xy_text = xy if isinstance(xy, str) else ""
        if len(xy_text) >= 1 and xy_text[0] != ".":
            staged_paths.add(path)
        if len(xy_text) >= 2 and xy_text[1] != ".":
            unstaged_paths.add(path)

    return (len(staged_paths), len(unstaged_paths), len(untracked_paths), len(unmerged_paths))


def load_context_payload(contexts_root: Path, context_id: str) -> dict[str, Any]:
    context_path = contexts_root / context_id / "context.json"
    return json.loads(context_path.read_text(encoding="utf-8"))


def load_dashboard_view(status_payload: dict[str, Any], repository_root: Path) -> DashboardView:
    branch = status_payload["branch"]["name"] if status_payload["branch"]["state"] == "attached" else "(detached)"
    upstream = status_payload["upstream"]
    operations = status_payload["git_operations"]

    return DashboardView(
        repository_name=repository_root.name,
        repository_root=str(repository_root),
        branch=branch,
        head=status_payload["head"],
        head_short=_short_sha(status_payload["head"]),
        head_subject=_run_git_subject(repository_root, status_payload["head"]),
        head_tags=_run_git_tags_at_head(repository_root, status_payload["head"]),
        workflow_state=status_payload["workflow_state"],
        staged_paths=status_payload["working_tree"]["staged"]["paths"],
        unstaged_paths=status_payload["working_tree"]["unstaged"]["paths"],
        untracked_paths=status_payload["working_tree"]["untracked"]["paths"],
        unmerged_paths=status_payload["working_tree"]["unmerged"]["paths"],
        staged_count=status_payload["working_tree"]["staged"]["count"],
        unstaged_count=status_payload["working_tree"]["unstaged"]["count"],
        untracked_count=status_payload["working_tree"]["untracked"]["count"],
        unmerged_count=status_payload["working_tree"]["unmerged"]["count"],
        git_operation=", ".join(operations) if operations else "None",
        upstream_ref=upstream["ref"] or "not configured",
        upstream_relation=upstream["relation"] or "unavailable",
        upstream_ahead=str(upstream["ahead"]) if upstream["ahead"] is not None else "unavailable",
        upstream_behind=str(upstream["behind"]) if upstream["behind"] is not None else "unavailable",
        current_snapshot_candidate_id=status_payload["current_snapshot_id_candidate"],
        matching_snapshot_id=status_payload["matching_snapshot_id"] or "none",
        remote_refresh_note="No remote refresh was performed.",
    )


def load_context_view(
    context_payload: dict[str, Any],
    current_git_state: dict[str, Any],
    current_repository_id: str,
    current_repository_root: Path,
) -> ContextView:
    branch = context_payload["branch"]["name"] if context_payload["branch"]["state"] == "attached" else "(detached)"
    freshness_status, freshness_detail = _context_freshness(
        context_payload=context_payload,
        current_git_state=current_git_state,
        current_repository_id=current_repository_id,
        current_repository_root=current_repository_root,
    )
    grouped_tests = _group_related_tests(context_payload.get("relevant_tests", []))
    return ContextView(
        context_id=context_payload["context_id"],
        repository_id=context_payload["repository_id"],
        repository_root=context_payload["repository_root"],
        branch=branch,
        head_commit=context_payload["head_commit"],
        head_short=_short_sha(context_payload["head_commit"]),
        working_tree_origin=_working_tree_origin_summary(context_payload.get("working_tree", {})),
        freshness_status=freshness_status,
        freshness_detail=freshness_detail,
        query=context_payload["original_query"],
        canonical_query=context_payload["canonical_query"],
        match_status=context_payload["match_status"],
        seed_count=len(context_payload.get("seed_matches", [])),
        selected_files=context_payload.get("selected_files", []),
        selected_symbols=context_payload.get("selected_symbols", []),
        relationships=context_payload.get("relevant_internal_relationships", []),
        tests=context_payload.get("relevant_tests", []),
        grouped_tests=grouped_tests,
        limitations=context_payload.get("relevant_limitations", []),
        parse_limitations=context_payload.get("parse_limitations", []),
        selection_metadata=context_payload.get("selection_metadata", {}),
    )


def list_snapshots(
    snapshots_root: Path,
    repository_id: str,
    current_snapshot_candidate_id: str,
    workflow_root: Path | None = None,
) -> list[SnapshotSummary]:
    rows: list[SnapshotSummary] = []
    classifications: dict[str, str] = {}
    if workflow_root is not None:
        plan_root = workflow_root / "commit_plans"
        for plan_id in _sorted_ids(plan_root):
            payload = json.loads((plan_root / plan_id / "plan.json").read_text(encoding="utf-8"))
            snapshot_id = payload.get("matching_snapshot_id")
            if snapshot_id:
                classifications[snapshot_id] = "Commit candidate"
        execution_root = workflow_root / "commit_executions"
        audit_root = workflow_root / "commit_audits"
        audits: dict[str, dict[str, Any]] = {}
        for audit_id in _sorted_ids(audit_root):
            audit_path = audit_root / audit_id / "audit.json"
            if audit_path.exists():
                audit = json.loads(audit_path.read_text(encoding="utf-8"))
                audits[audit.get("execution_id", "")] = audit
        for execution_id in _sorted_ids(execution_root):
            execution_path = execution_root / execution_id / "execution.json"
            if not execution_path.exists():
                continue
            execution = json.loads(execution_path.read_text(encoding="utf-8"))
            snapshot_id = execution.get("matching_snapshot_id")
            if not snapshot_id:
                continue
            outcome = audits.get(execution_id, {}).get("outcome")
            classifications[snapshot_id] = "Commit mutation succeeded / audit failed" if outcome == "mutation_succeeded_audit_failed" else "Commit aligned"
    for snapshot_id in _sorted_ids_by_mtime(snapshots_root):
        snapshot_dir = snapshots_root / snapshot_id
        snapshot_payload = _verify_existing_snapshot(snapshot_dir, repository_id)
        branch = snapshot_payload["branch"]["name"] if snapshot_payload["branch"]["state"] == "attached" else "(detached)"
        staged_count, unstaged_count, untracked_count, unmerged_count = _snapshot_working_tree_counts(snapshot_payload)
        rows.append(
            SnapshotSummary(
                snapshot_id=snapshot_id,
                head_commit=snapshot_payload["head_commit"],
                head_short=_short_sha(snapshot_payload["head_commit"]),
                branch=branch,
                working_tree_clean=bool(snapshot_payload["working_tree_clean"]),
                current_state_match=snapshot_id == current_snapshot_candidate_id,
                summary_label=_snapshot_label(
                    branch,
                    snapshot_payload["head_commit"],
                    bool(snapshot_payload["working_tree_clean"]),
                    snapshot_id == current_snapshot_candidate_id,
                ),
                classification=classifications.get(
                    snapshot_id,
                    "Matching Snapshot available" if snapshot_id == current_snapshot_candidate_id else "Intermediate",
                ),
                artifact_mtime_utc=_snapshot_mtime_utc(snapshot_dir),
                staged_count=staged_count,
                unstaged_count=unstaged_count,
                untracked_count=untracked_count,
                unmerged_count=unmerged_count,
            )
        )
    return rows


def list_comparisons(comparisons_root: Path, repository_id: str) -> list[ComparisonSummary]:
    rows: list[ComparisonSummary] = []
    for comparison_id in _sorted_ids_by_mtime(comparisons_root):
        comparison_dir = comparisons_root / comparison_id
        comparison_payload, _md = _verify_existing_comparison(comparison_dir)
        if comparison_payload.get("repository_id") != repository_id:
            continue
        rows.append(
            ComparisonSummary(
                comparison_id=comparison_id,
                before_snapshot_id=comparison_payload["before_snapshot_id"],
                after_snapshot_id=comparison_payload["after_snapshot_id"],
                before_label=_comparison_state_label(comparison_payload.get("before_repository_state", {})),
                after_label=_comparison_state_label(comparison_payload.get("after_repository_state", {})),
                aggregate_counts=comparison_payload.get("aggregate_counts", {}),
                artifact_mtime_utc=_snapshot_mtime_utc(comparison_dir),
            )
        )
    return rows


def load_comparison_payload(comparisons_root: Path, comparison_id: str) -> dict[str, Any]:
    comparison_dir = comparisons_root / comparison_id
    payload, _md = _verify_existing_comparison(comparison_dir)
    return payload


def list_analyses(analyses_root: Path, comparison_id: str) -> list[AnalysisSummary]:
    target_root = analyses_root / comparison_id
    rows: list[AnalysisSummary] = []
    for analysis_id in _sorted_ids(target_root):
        analysis_dir = target_root / analysis_id
        _analysis_input, analysis_json, _analysis_md = _verify_existing_analysis(analysis_dir)
        output = analysis_json["validated_model_output"]
        rows.append(
            AnalysisSummary(
                analysis_id=analysis_id,
                comparison_id=comparison_id,
                model_name=analysis_json["model_name"],
                provider=analysis_json["provider"],
                summary=output["summary"],
                summary_evidence_ids=output["summary_evidence_ids"],
                review_signals=output["review_signals"],
                questions_for_human_review=output["questions_for_human_review"],
            )
        )
    return rows


def load_analysis_summary(analyses_root: Path, comparison_id: str, analysis_id: str) -> AnalysisSummary:
    analysis_dir = analyses_root / comparison_id / analysis_id
    _analysis_input, analysis_json, _analysis_md = _verify_existing_analysis(analysis_dir)
    output = analysis_json["validated_model_output"]
    return AnalysisSummary(
        analysis_id=analysis_id,
        comparison_id=comparison_id,
        model_name=analysis_json["model_name"],
        provider=analysis_json["provider"],
        summary=output["summary"],
        summary_evidence_ids=output["summary_evidence_ids"],
        review_signals=output["review_signals"],
        questions_for_human_review=output["questions_for_human_review"],
    )


def list_context_ids(contexts_root: Path) -> list[str]:
    return _sorted_ids(contexts_root)


def snapshot_id_choices(snapshots_root: Path) -> list[str]:
    return _sorted_ids(snapshots_root)


def comparison_id_choices(comparisons_root: Path) -> list[str]:
    return _sorted_ids(comparisons_root)


def summarize_workflow_artifacts(workflow_root: Path, git_state: dict[str, Any]) -> list[WorkflowArtifactSummary]:
    rows: list[WorkflowArtifactSummary] = []
    branch_name = git_state["branch"]["name"] if git_state["branch"]["state"] == "attached" else "(detached)"
    head = git_state["head"]
    repository_root = Path(git_state["repository_root"])

    def _artifact_epoch(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return 0.0

    def _artifact_time(path: Path) -> str:
        try:
            return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat().replace("+00:00", "Z")
        except OSError:
            return "unknown"

    def _scope_paths(records: Any) -> list[str]:
        if not isinstance(records, list):
            return []
        paths = sorted(
            {
                row.get("path")
                for row in records
                if isinstance(row, dict) and isinstance(row.get("path"), str)
            },
            key=encode_path_order,
        )
        return paths

    def _git_identity(head_value: str) -> tuple[str, list[str]]:
        if not isinstance(head_value, str) or not head_value or head_value == "unknown":
            return ("(commit subject unavailable)", [])
        return (_run_git_subject(repository_root, head_value), _run_git_tags_at_head(repository_root, head_value))

    stage_executions_by_plan: dict[str, tuple[dict[str, Any], Path]] = {}
    stage_exec_root = workflow_root / "stage_executions"
    for execution_id in _sorted_ids(stage_exec_root):
        execution_dir = stage_exec_root / execution_id
        payload = json.loads((execution_dir / "execution.json").read_text(encoding="utf-8"))
        stage_executions_by_plan[payload.get("plan_id", "")] = (payload, execution_dir)

    commit_executions_by_plan: dict[str, tuple[dict[str, Any], Path]] = {}
    commit_exec_root = workflow_root / "commit_executions"
    for execution_id in _sorted_ids(commit_exec_root):
        execution_dir = commit_exec_root / execution_id
        payload = json.loads((execution_dir / "execution.json").read_text(encoding="utf-8"))
        commit_executions_by_plan[payload.get("plan_id", "")] = (payload, execution_dir)

    commit_audits_by_execution: dict[str, dict[str, Any]] = {}
    audit_root = workflow_root / "commit_audits"
    for audit_id in _sorted_ids(audit_root):
        audit_path = audit_root / audit_id / "audit.json"
        if audit_path.exists():
            audit_payload = json.loads(audit_path.read_text(encoding="utf-8"))
            commit_audits_by_execution[audit_payload.get("execution_id", "")] = audit_payload

    stage_plan_root = workflow_root / "stage_plans"
    for plan_id in _sorted_ids(stage_plan_root):
        plan_dir = stage_plan_root / plan_id
        payload = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        compatible = "compatible" if payload.get("head") == head and payload.get("branch", {}).get("name") == branch_name else "incompatible"
        execution_pair = stage_executions_by_plan.get(plan_id)
        head_subject, head_tags = _git_identity(payload.get("head") or "unknown")
        execution = None
        chronology_epoch = _artifact_epoch(plan_dir)
        chronology_time = _artifact_time(plan_dir)
        if execution_pair is not None:
            execution_payload, execution_dir = execution_pair
            execution_head = execution_payload.get("head_after") or "unknown"
            execution_subject, execution_tags = _git_identity(execution_head)
            execution = WorkflowExecutionSummary(
                execution_id=execution_payload.get("execution_id", "unknown"),
                status="Executed",
                result=f"Resulting workflow state: {execution_payload.get('resulting_workflow_state', 'unknown')}",
                artifact_mtime_utc=_artifact_time(execution_dir),
                head_after=execution_head,
                head_after_short=_short_sha(execution_head),
                head_after_subject=execution_subject,
                head_after_tags=execution_tags,
            )
            chronology_epoch = max(chronology_epoch, _artifact_epoch(execution_dir))
            chronology_time = _artifact_time(execution_dir)
        rows.append(
            WorkflowArtifactSummary(
                family="STAGE",
                plan=WorkflowPlanSummary(
                    plan_id=plan_id,
                    kind="stage_plan",
                    action="Prepare Stage",
                    branch=payload.get("branch", {}).get("name") or "(detached)",
                    head=payload.get("head") or "unknown",
                    head_short=_short_sha(payload.get("head") or "unknown"),
                    head_subject=head_subject,
                    head_tags=head_tags,
                    count=int(payload.get("candidate_record_count", 0)),
                    scope_paths=_scope_paths(payload.get("candidate_records")),
                    artifact_mtime_utc=_artifact_time(plan_dir),
                    compatibility=compatible,
                ),
                execution=execution,
                chronology_time_utc=chronology_time,
                chronology_epoch=chronology_epoch,
            )
        )

    commit_plan_root = workflow_root / "commit_plans"
    for plan_id in _sorted_ids(commit_plan_root):
        plan_dir = commit_plan_root / plan_id
        payload = json.loads((plan_dir / "plan.json").read_text(encoding="utf-8"))
        compatible = "compatible" if payload.get("head_before") == head and payload.get("branch", {}).get("name") == branch_name else "incompatible"
        execution_pair = commit_executions_by_plan.get(plan_id)
        head_subject, head_tags = _git_identity(payload.get("head_before") or "unknown")
        execution = None
        chronology_epoch = _artifact_epoch(plan_dir)
        chronology_time = _artifact_time(plan_dir)
        if execution_pair is not None:
            execution_payload, execution_dir = execution_pair
            execution_head = execution_payload.get("head_after") or "unknown"
            execution_subject, execution_tags = _git_identity(execution_head)
            audit = commit_audits_by_execution.get(execution_payload.get("execution_id", ""), {})
            audit_failed = audit.get("outcome") == "mutation_succeeded_audit_failed"
            execution = WorkflowExecutionSummary(
                execution_id=execution_payload.get("execution_id", "unknown"),
                status="Mutation succeeded / audit failed" if audit_failed else "Executed",
                result=(
                    f"Commit mutation succeeded; post-verification failed: {audit.get('reason', 'audit failure')}. Do not retry automatically; inspect the resulting HEAD."
                    if audit_failed
                    else f"Committed to {_short_sha(execution_payload.get('head_after', 'unknown'))}"
                ),
                artifact_mtime_utc=_artifact_time(execution_dir),
                head_after=execution_head,
                head_after_short=_short_sha(execution_head),
                head_after_subject=execution_subject,
                head_after_tags=execution_tags,
            )
            chronology_epoch = max(chronology_epoch, _artifact_epoch(execution_dir))
            chronology_time = _artifact_time(execution_dir)
        rows.append(
            WorkflowArtifactSummary(
                family="COMMIT",
                plan=WorkflowPlanSummary(
                    plan_id=plan_id,
                    kind="commit_plan",
                    action="Prepare Commit",
                    branch=payload.get("branch", {}).get("name") or "(detached)",
                    head=payload.get("head_before") or "unknown",
                    head_short=_short_sha(payload.get("head_before") or "unknown"),
                    head_subject=head_subject,
                    head_tags=head_tags,
                    count=int(payload.get("staged_record_count", 0)),
                    scope_paths=_scope_paths(payload.get("staged_records")),
                    artifact_mtime_utc=_artifact_time(plan_dir),
                    compatibility=compatible,
                ),
                execution=execution,
                chronology_time_utc=chronology_time,
                chronology_epoch=chronology_epoch,
            )
        )

    return sorted(rows, key=lambda row: (-row.chronology_epoch, row.plan.plan_id.encode("utf-8")))


def relation_reason(rel: dict[str, Any]) -> str:
    if rel["relationship_type"] == "test_reference":
        return "related test"
    if rel["relationship_type"] == "module_dependency":
        return "direct relationship"
    if rel["relationship_type"] == "imported_symbol":
        return "direct relationship"
    if rel["relationship_type"] == "call":
        return "direct relationship"
    return "relationship"


def file_reason(file_row: dict[str, Any]) -> str:
    reasons = file_row.get("reasons", [])
    if not reasons:
        return "selected by deterministic policy"
    reason_text = reasons[0]
    if "exact" in reason_text or "substring" in reason_text or "token" in reason_text:
        return "lexical match"
    return reason_text


def match_label(match_status: str) -> str:
    return "MATCHED" if match_status == MATCH_STATUS_MATCHED else "NO MATCHES"
