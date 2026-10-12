from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from flask import Flask, Response, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.exceptions import HTTPException

from ..analysis.manager import AnalysisError, analyze_comparison
from ..compare.manager import compare_snapshots
from ..context.generator import build_and_publish_context
from ..scanner.core import DEFAULT_STATE_ROOT, run_scan_with_artifacts
from ..scanner.git_ops import ScanError
from ..snapshot.manager import create_snapshot
from ..workflow.errors import WorkflowReasonError
from ..workflow.git_state import inspect_git_state
from ..workflow.diff import GitReviewError, inspect_git_review
from ..workflow.git_actions import GitActionError, PreparedGitActions
from ..workflow.git_history import (
    GitHistoryError,
    inspect_branches,
    inspect_file_history,
    inspect_history,
)
from ..workflow.handoff_packets import HandoffPacketError, generate_handoff_packet
from ..workflow.milestones import (
    MilestoneArtifactError,
    discover_milestones,
    load_milestone_selection,
    save_milestone_selection,
)
from ..workflow.lifecycle import WorkflowRecordError, load_milestone_workflow, record_milestone_event
from ..workflow.status import WorkflowError, generate_milestone_status
from .architect_contract import (
    MAX_PACKET_BYTES,
    ArchitectContractError,
    build_architect_response,
)
from .repository_registry import RepositoryEntry, RepositoryRegistry, RepositoryRegistryError
from .views import (
    comparison_id_choices,
    file_reason,
    list_analyses,
    list_comparisons,
    list_context_ids,
    list_snapshots,
    load_analysis_summary,
    load_comparison_payload,
    load_context_payload,
    load_context_view,
    load_dashboard_view,
    match_label,
    relation_reason,
    snapshot_id_choices,
    summarize_workflow_artifacts,
)

ALLOWED_LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
MAX_CONTEXT_QUERY_CHARS = 400


class WebUIError(RuntimeError):
    def __init__(self, code: str, safe_message: str, status_code: int = 400) -> None:
        self.code = code
        self.safe_message = safe_message
        self.status_code = status_code
        super().__init__(safe_message)


def _validate_host(host: str) -> str:
    candidate = host.strip()
    if candidate not in ALLOWED_LOOPBACK_HOSTS:
        raise ValueError("non-loopback host binding is blocked in milestone 009; use 127.0.0.1 or localhost")
    return candidate


def _validate_context_query(query: str) -> str:
    value = query.strip()
    if not value:
        raise WebUIError("invalid_input", "Context query is required.")
    if len(value) > MAX_CONTEXT_QUERY_CHARS:
        raise WebUIError("invalid_input", f"Context query exceeds maximum length ({MAX_CONTEXT_QUERY_CHARS}).")
    for char in value:
        if ord(char) < 32 or ord(char) == 127:
            raise WebUIError("invalid_input", "Context query contains unsupported control characters.")
    return value


def _require_csrf() -> None:
    expected = session.get("csrf_token")
    supplied = request.form.get("csrf_token", "")
    if not expected or not supplied or not secrets.compare_digest(expected, supplied):
        raise WebUIError("invalid_request_token", "Invalid request token.")


def _ensure_csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _read_status(status_json_path: Path) -> dict[str, Any]:
    import json

    return json.loads(status_json_path.read_text(encoding="utf-8"))


def _architect_json_response(payload: dict[str, Any], status_code: int = 200) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(body.encode("utf-8")) > MAX_PACKET_BYTES:
        payload = {
            "contract_version": "1",
            "error": {
                "code": "response_oversized",
                "message": f"Architect response exceeds the {MAX_PACKET_BYTES}-byte limit.",
            },
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        status_code = 413
    response = Response(body, status=status_code, mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


def create_web_app(
    *,
    repository_path: str | None = None,
    repository_registry_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    state_root: Path | None = None,
) -> Flask:
    bound_host = _validate_host(host)
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")

    if (repository_path is None) == (repository_registry_path is None):
        raise ValueError("Provide exactly one of repository_path or repository_registry_path.")
    registry = (
        RepositoryRegistry.from_toml(repository_registry_path)
        if repository_registry_path is not None
        else RepositoryRegistry.single(repository_path)
    )
    initial_repository = registry.active()
    effective_state_root = (state_root or DEFAULT_STATE_ROOT).expanduser()
    git_actions = PreparedGitActions()
    repository_action_lock = threading.RLock()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    app.config["REPO_ROOT"] = initial_repository.path
    app.config["REPO_ID"] = initial_repository.repository_id
    app.config["REPOSITORY_REGISTRY_PATH"] = (
        str(Path(repository_registry_path).expanduser().resolve())
        if repository_registry_path is not None
        else None
    )
    app.config["STATE_ROOT"] = effective_state_root
    app.config["BOUND_HOST"] = bound_host
    app.config["BOUND_PORT"] = port

    @app.before_request
    def _bind_active_repository() -> None:
        g.active_repository, g.repository_registry_generation = registry.snapshot()

    def current_repository() -> RepositoryEntry:
        return g.active_repository

    def require_current_repository_selection() -> None:
        active_repository, generation = registry.snapshot()
        if (
            generation != g.repository_registry_generation
            or active_repository.key != current_repository().key
        ):
            raise WebUIError(
                "stale_repository_selection",
                "The active repository changed during this request. Review the current repository and retry.",
                status_code=409,
            )

    @app.context_processor
    def _template_context() -> dict[str, Any]:
        active_repository = g.active_repository
        return {
            "csrf_token": _ensure_csrf_token(),
            "repository_name": active_repository.name,
            "repository_root": str(active_repository.path),
            "repository_choices": registry.entries,
            "active_repository_key": active_repository.key,
            "repository_registry_generation": g.repository_registry_generation,
            "bound_host": bound_host,
            "bound_port": port,
        }

    def status_paths() -> tuple[Path, Path]:
        workflow_root = effective_state_root / current_repository().repository_id / "workflow"
        return workflow_root / "status.json", workflow_root

    def contexts_root() -> Path:
        return effective_state_root / current_repository().repository_id / "contexts"

    def snapshots_root() -> Path:
        return effective_state_root / current_repository().repository_id / "snapshots"

    def comparisons_root() -> Path:
        return effective_state_root / current_repository().repository_id / "comparisons"

    def analyses_root() -> Path:
        return effective_state_root / current_repository().repository_id / "analyses"

    def workflow_root() -> Path:
        return effective_state_root / current_repository().repository_id / "workflow"

    # Prime deterministic status on startup so dashboard can render immediately.
    generate_milestone_status(str(initial_repository.path), state_root=effective_state_root)

    @app.errorhandler(WebUIError)
    def _handle_web_error(err: WebUIError):
        if request.path.startswith("/api/architect/"):
            return _architect_json_response(
                {
                    "contract_version": "1",
                    "error": {"code": err.code, "message": err.safe_message},
                },
                err.status_code,
            )
        return render_template(
            "error.html",
            error_code=err.code,
            error_message=err.safe_message,
            mutation_note="NO TARGET GIT MUTATION",
        ), err.status_code

    @app.errorhandler(WorkflowReasonError)
    def _handle_workflow_reason_error(err: WorkflowReasonError):
        if request.path.startswith("/api/architect/"):
            return _architect_json_response(
                {
                    "contract_version": "1",
                    "error": {"code": err.code, "message": err.safe_message},
                },
                400,
            )
        return render_template(
            "error.html",
            error_code=err.code,
            error_message=err.safe_message,
            mutation_note="NO TARGET GIT MUTATION",
        ), 400

    @app.errorhandler(GitActionError)
    def _handle_git_action_error(err: GitActionError):
        uncertain = err.code in {
            "git_action_execution_failed",
            "git_action_output_incomplete",
            "git_commit_failed",
            "post_action_verification_failed",
            "post_commit_verification_failed",
            "remote_action_execution_failed",
            "remote_verification_failed",
        }
        note = (
            "Git action outcome is not verified; inspect Git Review before retrying."
            if uncertain
            else "No Git action was executed."
        )
        return render_template(
            "error.html",
            error_code=err.code,
            error_message=str(err),
            mutation_note=note,
        ), err.status_code

    @app.errorhandler(ScanError)
    @app.errorhandler(WorkflowError)
    @app.errorhandler(AnalysisError)
    def _handle_expected_service_error(err: Exception):
        if request.path.startswith("/api/architect/"):
            return _architect_json_response(
                {
                    "contract_version": "1",
                    "error": {"code": "service_error", "message": str(err)},
                },
                400,
            )
        return render_template(
            "error.html",
            error_code="service_error",
            error_message=str(err),
            mutation_note="NO TARGET GIT MUTATION",
        ), 400

    @app.errorhandler(Exception)
    def _handle_unexpected_error(err: Exception):
        if request.path.startswith("/api/architect/"):
            if isinstance(err, HTTPException):
                return _architect_json_response(
                    {
                        "contract_version": "1",
                        "error": {"code": err.name.lower().replace(" ", "_"), "message": err.description},
                    },
                    err.code or 500,
                )
            return _architect_json_response(
                {
                    "contract_version": "1",
                    "error": {
                        "code": "unexpected_error",
                        "message": "Unexpected Architect contract error.",
                    },
                },
                500,
            )
        if isinstance(err, HTTPException):
            return render_template(
                "error.html",
                error_code=err.name,
                error_message=err.description,
                mutation_note="NO TARGET GIT MUTATION",
            ), err.code
        return render_template(
            "error.html",
            error_code="unexpected_error",
            error_message="Unexpected application error.",
            mutation_note="NO TARGET GIT MUTATION",
        ), 500

    @app.errorhandler(ArchitectContractError)
    def _handle_architect_contract_error(err: ArchitectContractError):
        return _architect_json_response(
            {
                "contract_version": "1",
                "error": {"code": err.code, "message": str(err)},
            },
            err.status_code,
        )

    def architect_query_value(name: str, *, maximum_length: int) -> str | None:
        values = request.args.getlist(name)
        if not values:
            return None
        if len(values) != 1 or not values[0] or len(values[0]) > maximum_length:
            raise ArchitectContractError(
                "invalid_query_parameter",
                f"Query parameter {name!r} must be supplied once and within its length limit.",
                400,
            )
        return values[0]

    def architect_endpoint(resource: str, role: str | None = None) -> Response:
        allowed_parameters = {
            "expected_repository_key",
            "expected_generation",
            "expected_milestone_id",
        }
        unknown_parameters = set(request.args) - allowed_parameters
        if unknown_parameters:
            raise ArchitectContractError(
                "unsupported_query_parameter",
                f"Unsupported query parameter: {sorted(unknown_parameters)[0]!r}.",
                400,
            )
        try:
            expected_repository_key = architect_query_value("expected_repository_key", maximum_length=64)
            expected_generation_text = architect_query_value("expected_generation", maximum_length=12)
            expected_milestone_id = architect_query_value("expected_milestone_id", maximum_length=32)
            if expected_generation_text is not None:
                try:
                    expected_generation = int(expected_generation_text)
                except ValueError as exc:
                    raise ArchitectContractError(
                        "invalid_query_parameter",
                        "Query parameter 'expected_generation' must be a non-negative integer.",
                        400,
                    ) from exc
                if expected_generation < 0:
                    raise ArchitectContractError(
                        "invalid_query_parameter",
                        "Query parameter 'expected_generation' must be a non-negative integer.",
                        400,
                    )
            else:
                expected_generation = None

            with repository_action_lock:
                active_repository, active_generation = registry.snapshot()
                if (
                    active_repository.key != current_repository().key
                    or active_generation != g.repository_registry_generation
                ):
                    raise ArchitectContractError(
                        "stale_repository_selection",
                        "The active repository changed during this request; refresh status and retry.",
                        409,
                    )
                payload = build_architect_response(
                    repository=current_repository(),
                    registry_generation=g.repository_registry_generation,
                    state_root=effective_state_root,
                    resource=resource,
                    role=role,
                    expected_repository_key=expected_repository_key,
                    expected_generation=expected_generation,
                    expected_milestone_id=expected_milestone_id,
                )
                active_repository, active_generation = registry.snapshot()
                if (
                    active_repository.key != current_repository().key
                    or active_generation != g.repository_registry_generation
                ):
                    raise ArchitectContractError(
                        "stale_repository_selection",
                        "The active repository changed during this request; refresh status and retry.",
                        409,
                    )
                return _architect_json_response(payload)
        except ArchitectContractError as exc:
            return _architect_json_response(
                {
                    "contract_version": "1",
                    "error": {"code": exc.code, "message": str(exc)},
                },
                exc.status_code,
            )

    @app.get("/api/architect/v1/status")
    def architect_status():
        return architect_endpoint("status")

    @app.get("/api/architect/v1/milestone")
    def architect_milestone():
        return architect_endpoint("milestone")

    @app.get("/api/architect/v1/prompt")
    def architect_prompt():
        return architect_endpoint("prompt")

    @app.get("/api/architect/v1/workflow")
    def architect_workflow():
        return architect_endpoint("workflow")

    @app.get("/api/architect/v1/closeout")
    def architect_closeout():
        return architect_endpoint("closeout")

    @app.get("/api/architect/v1/git")
    def architect_git():
        return architect_endpoint("git")

    @app.get("/api/architect/v1/evidence")
    def architect_evidence():
        return architect_endpoint("evidence")

    @app.get("/api/architect/v1/documents")
    def architect_documents():
        return architect_endpoint("documents")

    @app.get("/api/architect/v1/documents/<role>")
    def architect_document(role: str):
        return architect_endpoint("documents", role=role)

    @app.get("/api/architect/v1/review")
    def architect_review():
        return architect_endpoint("review")

    @app.get("/healthz")
    def health_check():
        active_repository = registry.active()
        return jsonify(
            {
                "service": "repo-control",
                "active_repository": active_repository.key,
            }
        )

    @app.post("/repositories/select")
    def select_repository():
        _require_csrf()
        requested_key = request.form.get("repository_key", "")
        with repository_action_lock:
            try:
                selected_repository, changed = registry.select(requested_key)
            except RepositoryRegistryError as exc:
                raise WebUIError("invalid_repository_selection", str(exc), 400) from exc
            if changed:
                git_actions.invalidate_all()
                app.config["REPO_ROOT"] = selected_repository.path
                app.config["REPO_ID"] = selected_repository.repository_id
                flash(
                    f"Active repository changed to {selected_repository.name}. Pending prepared Git actions were invalidated.",
                    "success",
                )
            else:
                flash(f"{selected_repository.name} is already the active repository.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/")
    def dashboard():
        status_json_path, _ = status_paths()
        if not status_json_path.exists():
            generate_milestone_status(str(current_repository().path), state_root=effective_state_root)
        status_payload = _read_status(status_json_path)
        dashboard_view = load_dashboard_view(status_payload, current_repository().path)
        return render_template("dashboard.html", dashboard=dashboard_view)

    @app.post("/status/refresh")
    def refresh_status():
        _require_csrf()
        generate_milestone_status(str(current_repository().path), state_root=effective_state_root)
        flash("Status refreshed.", "success")
        return redirect(url_for("dashboard"))

    @app.get("/context")
    def context_page():
        selected_context_id = request.args.get("context_id")
        context_ids = list_context_ids(contexts_root())
        context_view = None
        if selected_context_id:
            if selected_context_id not in context_ids:
                raise WebUIError("context_not_found", "Context result was not found.", status_code=404)
            current_git_state = inspect_git_state(str(current_repository().path))
            context_view = load_context_view(
                load_context_payload(contexts_root(), selected_context_id),
                current_git_state,
                current_repository().repository_id,
                current_repository().path,
            )

        return render_template(
            "context.html",
            context_ids=context_ids,
            selected_context_id=selected_context_id,
            context_view=context_view,
            match_label=match_label,
            file_reason=file_reason,
            relation_reason=relation_reason,
            max_query_chars=MAX_CONTEXT_QUERY_CHARS,
        )

    @app.post("/context/generate")
    def generate_context():
        _require_csrf()
        query = _validate_context_query(request.form.get("query", ""))
        scan_result = run_scan_with_artifacts(str(current_repository().path), state_root=effective_state_root)
        result = build_and_publish_context(scan_result=scan_result, query=query)
        flash("Context generated.", "success")
        return redirect(url_for("context_page", context_id=result["context_id"]))

    @app.get("/snapshots")
    def snapshots_page():
        selected_snapshot_id = request.args.get("snapshot_id")
        status_json_path, wf_root = status_paths()
        # Keep current-match cue aligned with the repository's current state.
        generate_milestone_status(str(current_repository().path), state_root=effective_state_root)
        status_payload = _read_status(status_json_path)
        rows = list_snapshots(
            snapshots_root(),
            current_repository().repository_id,
            status_payload["current_snapshot_id_candidate"],
            workflow_root=wf_root,
        )
        selected_snapshot = next((row for row in rows if row.snapshot_id == selected_snapshot_id), None)
        return render_template(
            "snapshots.html",
            snapshots=rows,
            selected_snapshot_id=selected_snapshot_id,
            selected_snapshot=selected_snapshot,
        )

    @app.post("/snapshots/create")
    def create_snapshot_action():
        _require_csrf()
        scan_result = run_scan_with_artifacts(str(current_repository().path), state_root=effective_state_root)
        result = create_snapshot(scan_result=scan_result, state_root=effective_state_root)
        if result["reused_existing"]:
            flash("Snapshot already exists for this exact repository state; reused existing snapshot.", "success")
        else:
            flash("Snapshot captured.", "success")
        return redirect(url_for("snapshots_page", snapshot_id=result["snapshot_id"]))

    @app.get("/comparisons")
    def comparisons_page():
        selected_comparison_id = request.args.get("comparison_id")
        selected_before_snapshot_id = request.args.get("before_snapshot_id")
        selected_after_snapshot_id = request.args.get("after_snapshot_id")

        status_json_path, _ = status_paths()
        generate_milestone_status(str(current_repository().path), state_root=effective_state_root)
        status_payload = _read_status(status_json_path)

        snapshot_rows = list_snapshots(
            snapshots_root(),
            current_repository().repository_id,
            status_payload["current_snapshot_id_candidate"],
        )
        snapshots_by_id = {row.snapshot_id: row for row in snapshot_rows}
        comparisons = list_comparisons(comparisons_root(), current_repository().repository_id)
        comparisons_by_id = {row.comparison_id: row for row in comparisons}
        selected_payload = None
        if selected_comparison_id:
            allowed_ids = {row.comparison_id for row in comparisons}
            if selected_comparison_id not in allowed_ids:
                raise WebUIError("comparison_not_found", "Comparison result was not found.", status_code=404)
            selected_payload = load_comparison_payload(comparisons_root(), selected_comparison_id)

        if selected_payload is not None:
            if not selected_before_snapshot_id:
                selected_before_snapshot_id = selected_payload.get("before_snapshot_id")
            if not selected_after_snapshot_id:
                selected_after_snapshot_id = selected_payload.get("after_snapshot_id")

        if snapshot_rows:
            if selected_before_snapshot_id not in snapshots_by_id:
                selected_before_snapshot_id = snapshot_rows[0].snapshot_id
            if selected_after_snapshot_id not in snapshots_by_id:
                selected_after_snapshot_id = snapshot_rows[0].snapshot_id

        selected_before_snapshot = snapshots_by_id.get(selected_before_snapshot_id or "")
        selected_after_snapshot = snapshots_by_id.get(selected_after_snapshot_id or "")

        selected_comparison_summary = None
        if selected_comparison_id:
            selected_comparison_summary = comparisons_by_id.get(selected_comparison_id)

        selectors_match_selected_comparison = True
        if selected_payload is not None:
            selectors_match_selected_comparison = (
                selected_before_snapshot_id == selected_payload.get("before_snapshot_id")
                and selected_after_snapshot_id == selected_payload.get("after_snapshot_id")
            )

        return render_template(
            "comparisons.html",
            snapshot_options=snapshot_rows,
            comparisons=comparisons,
            selected_comparison=selected_payload,
            selected_comparison_id=selected_comparison_id,
            selected_comparison_summary=selected_comparison_summary,
            selected_before_snapshot_id=selected_before_snapshot_id,
            selected_after_snapshot_id=selected_after_snapshot_id,
            selected_before_snapshot=selected_before_snapshot,
            selected_after_snapshot=selected_after_snapshot,
            selectors_match_selected_comparison=selectors_match_selected_comparison,
        )

    @app.post("/comparisons/create")
    def create_comparison_action():
        _require_csrf()
        before_snapshot_id = request.form.get("before_snapshot_id", "")
        after_snapshot_id = request.form.get("after_snapshot_id", "")
        valid_snapshot_ids = set(snapshot_id_choices(snapshots_root()))
        if before_snapshot_id not in valid_snapshot_ids or after_snapshot_id not in valid_snapshot_ids:
            raise WebUIError("invalid_input", "Snapshot selection must reference known snapshot IDs.")
        result = compare_snapshots(
            before_snapshot_id,
            after_snapshot_id,
            str(current_repository().path),
            state_root=effective_state_root,
        )
        if result["reused_existing"]:
            flash("Comparison already exists for this exact before/after snapshot pair; reused existing comparison.", "success")
        else:
            flash("Comparison created.", "success")
        return redirect(
            url_for(
                "comparisons_page",
                comparison_id=result["comparison_id"],
                before_snapshot_id=before_snapshot_id,
                after_snapshot_id=after_snapshot_id,
            )
        )

    @app.get("/analysis")
    def analysis_page():
        comparisons = list_comparisons(comparisons_root(), current_repository().repository_id)
        comparison_lookup = {row.comparison_id: row for row in comparisons}
        selected_comparison_id = request.args.get("comparison_id")
        selected_analysis_id = request.args.get("analysis_id")
        analyses = []
        selected_analysis = None

        if selected_comparison_id:
            if selected_comparison_id not in comparison_lookup:
                raise WebUIError("comparison_not_found", "Comparison result was not found.", status_code=404)
            analyses = list_analyses(analyses_root(), selected_comparison_id)
            if selected_analysis_id:
                analysis_ids = {row.analysis_id for row in analyses}
                if selected_analysis_id not in analysis_ids:
                    raise WebUIError("analysis_not_found", "Analysis result was not found.", status_code=404)
                selected_analysis = load_analysis_summary(analyses_root(), selected_comparison_id, selected_analysis_id)

        return render_template(
            "analysis.html",
            comparisons=comparisons,
            selected_comparison_id=selected_comparison_id,
            analyses=analyses,
            selected_analysis=selected_analysis,
        )

    @app.post("/analysis/run")
    def analysis_run_action():
        _require_csrf()
        comparison_id = request.form.get("comparison_id", "")
        valid_ids = set(comparison_id_choices(comparisons_root()))
        if comparison_id not in valid_ids:
            raise WebUIError("invalid_input", "Comparison selection must reference a known comparison ID.")
        result = analyze_comparison(comparison_id, str(current_repository().path), state_root=effective_state_root)
        flash("Local GPT-OSS analysis completed.", "success")
        return redirect(url_for("analysis_page", comparison_id=comparison_id, analysis_id=result["analysis_id"]))

    @app.get("/git-review")
    def git_review_page():
        review_view = request.args.get("view", "changes")
        selected_file = request.args.get("file")
        preview_action = request.args.get("preview")
        error_message = None
        review = None
        history = None
        file_history = None
        branches = None
        branch_controls = None
        remote_controls = None
        status_code = 200
        file_action_options = {}
        commit_options = {"available": False, "reason": "Commit is available when supported changes are staged."}
        try:
            if review_view == "changes":
                review = inspect_git_review(
                    str(current_repository().path),
                    selected_file=selected_file,
                    preview_action=preview_action,
                )
                file_action_options = git_actions.options_for_tokens(
                    str(current_repository().path),
                    [item["token"] for item in review["files"]],
                )
                commit_options = git_actions.can_commit(str(current_repository().path))
            elif review_view == "history":
                history = inspect_history(
                    str(current_repository().path),
                    selected_commit=request.args.get("commit"),
                )
                file_token = request.args.get("file_history")
                if file_token:
                    file_history = inspect_file_history(str(current_repository().path), file_token)
            elif review_view == "branches":
                branches = inspect_branches(
                    str(current_repository().path),
                    selected_branch=request.args.get("branch"),
                )
                branch_controls = git_actions.branch_controls(
                    str(current_repository().path),
                    request.args.get("branch"),
                )
                remote_controls = git_actions.remote_controls(str(current_repository().path))
            else:
                raise ValueError("Git Review view must be changes, history, or branches.")
        except FileNotFoundError as exc:
            raise WebUIError("git_selection_not_found", str(exc), status_code=404) from exc
        except ValueError as exc:
            raise WebUIError("invalid_git_selection", str(exc), status_code=400) from exc
        except (GitReviewError, GitHistoryError, GitActionError) as exc:
            error_message = str(exc)
            status_code = 503
        return render_template(
            "git_review.html",
            review_view=review_view,
            review=review,
            history=history,
            file_history=file_history,
            branches=branches,
            branch_controls=branch_controls,
            remote_controls=remote_controls,
            selected_commit=request.args.get("commit"),
            selected_file_history=request.args.get("file_history"),
            selected_branch=request.args.get("branch"),
            file_action_options=file_action_options,
            commit_options=commit_options,
            error_message=error_message,
            selected_file=selected_file,
        ), status_code

    def require_mutation_request() -> None:
        _require_csrf()
        if request.headers.get("Sec-Fetch-Site", "").casefold() == "cross-site":
            raise WebUIError("cross_origin_request", "Cross-origin Git action requests are blocked.", status_code=403)
        origin = request.headers.get("Origin")
        referer = request.headers.get("Referer")
        supplied = origin or referer
        if supplied:
            parsed = urlsplit(supplied)
            if parsed.scheme != request.scheme or parsed.netloc != request.host:
                raise WebUIError("cross_origin_request", "Cross-origin Git action requests are blocked.", status_code=403)

    def redirect_after_git_action(action: str):
        branch_actions = {
            "create-branch",
            "switch-branch",
            "delete-branch",
            "fetch",
            "push",
            "publish-branch",
            "fast-forward",
        }
        view = "branches" if action in branch_actions else "changes"
        return redirect(url_for("git_review_page", view=view))

    @app.post("/git-review/action/prepare")
    def git_action_prepare():
        require_mutation_request()
        action = request.form.get("action", "")
        with repository_action_lock:
            require_current_repository_selection()
            token, preview = git_actions.prepare(
                str(current_repository().path),
                action,
                path_tokens=request.form.getlist("path_token"),
                message=request.form.get("commit_message"),
                branch_token=request.form.get("branch_token"),
                branch_name=request.form.get("branch_name"),
                remote_token=request.form.get("remote_token"),
            )
        return render_template(
            "git_action_confirm.html",
            token=token,
            preview=preview,
            csrf_token=_ensure_csrf_token(),
            destructive=action in {"restore-unstaged", "restore-head", "delete-branch"},
        )

    @app.post("/git-review/action/confirm")
    def git_action_confirm():
        require_mutation_request()
        action = request.form.get("action", "")
        token = request.form.get("token", "")
        with repository_action_lock:
            require_current_repository_selection()
            result = git_actions.confirm(str(current_repository().path), action, token)
        flash(result["message"], "success")
        return redirect_after_git_action(result["action"])

    @app.post("/git-review/action/cancel")
    def git_action_cancel():
        require_mutation_request()
        action = request.form.get("action", "")
        git_actions.cancel(request.form.get("token", ""))
        flash("Prepared action canceled; no Git change was made.", "success")
        return redirect_after_git_action(action)

    @app.get("/workflow")
    def workflow_page():
        status_json_path, wf_root = status_paths()
        generate_milestone_status(str(current_repository().path), state_root=effective_state_root)
        status_payload = _read_status(status_json_path)
        git_state = inspect_git_state(str(current_repository().path))
        workflow_rows = summarize_workflow_artifacts(wf_root, git_state)
        return render_template(
            "workflow.html",
            workflow_state=status_payload["workflow_state"],
            matching_snapshot_exists=status_payload["matching_snapshot_exists"],
            matching_snapshot_id=status_payload["matching_snapshot_id"],
            rows=workflow_rows,
        )

    @app.get("/milestones")
    def milestone_page():
        repository = current_repository()
        try:
            inventory = discover_milestones(repository.path, repository.milestone_root)
            selection = load_milestone_selection(
                repository.path,
                effective_state_root,
                repository.milestone_root,
            )
        except MilestoneArtifactError as exc:
            raise WebUIError("milestone_discovery_failed", str(exc), status_code=503) from exc
        workflow = None
        if selection["status"] == "selected":
            try:
                workflow = load_milestone_workflow(
                    repository.path,
                    effective_state_root,
                    repository.milestone_root,
                    selection["selection"],
                )
            except (MilestoneArtifactError, WorkflowRecordError) as exc:
                workflow = {"status": "invalid", "reason": str(exc)}
        return render_template(
            "milestones.html",
            candidates=inventory["candidates"],
            inventory_issues=inventory["issues"],
            selection=selection,
            milestone_root=repository.milestone_root,
            workflow=workflow,
        )

    @app.post("/milestones/select")
    def select_milestone():
        _require_csrf()
        with repository_action_lock:
            require_current_repository_selection()
            submitted_repository = request.form.get("repository_key", "")
            submitted_generation = request.form.get("repository_generation", "")
            try:
                generation = int(submitted_generation)
            except ValueError as exc:
                raise WebUIError("stale_milestone_selection", "Refresh the milestone page and select again.", 409) from exc
            if (
                submitted_repository != current_repository().key
                or generation != g.repository_registry_generation
            ):
                raise WebUIError(
                    "stale_milestone_selection",
                    "The selected repository changed since this page was rendered. Refresh and select a milestone again.",
                    status_code=409,
                )
            try:
                candidate = save_milestone_selection(
                    current_repository().path,
                    effective_state_root,
                    request.form.get("milestone_id", ""),
                    current_repository().milestone_root,
                )
            except MilestoneArtifactError as exc:
                raise WebUIError("invalid_milestone_selection", str(exc), status_code=400) from exc
        flash(
            f"Selected {candidate['milestone_id']} for {current_repository().name}.",
            "success",
        )
        return redirect(url_for("milestone_page"))

    @app.post("/milestones/workflow/event")
    def record_milestone_workflow_event():
        require_mutation_request()
        with repository_action_lock:
            require_current_repository_selection()
            submitted_repository = request.form.get("repository_key", "")
            submitted_generation = request.form.get("repository_generation", "")
            try:
                generation = int(submitted_generation)
            except ValueError as exc:
                raise WebUIError(
                    "stale_workflow_request",
                    "Refresh the milestone page and retry the workflow action.",
                    status_code=409,
                ) from exc
            if (
                submitted_repository != current_repository().key
                or generation != g.repository_registry_generation
            ):
                raise WebUIError(
                    "stale_workflow_request",
                    "The selected repository changed since this page was rendered. Refresh and retry.",
                    status_code=409,
                )
            repository = current_repository()
            selection = load_milestone_selection(
                repository.path,
                effective_state_root,
                repository.milestone_root,
            )
            expected_id = request.form.get("milestone_id", "")
            expected_prompt_path = request.form.get("prompt_path", "")
            if (
                selection["status"] != "selected"
                or selection["selection"]["milestone_id"] != expected_id
                or selection["selection"]["prompt_path"] != expected_prompt_path
            ):
                raise WebUIError(
                    "stale_milestone_workflow_request",
                    "The selected milestone changed or is invalid. Refresh the page and retry.",
                    status_code=409,
                )
            try:
                record_milestone_event(
                    repository.path,
                    effective_state_root,
                    repository.milestone_root,
                    selection["selection"],
                    event_type=request.form.get("event_type", ""),
                    summary=request.form.get("summary", ""),
                    actor_role=request.form.get("actor_role", ""),
                    po_approved=request.form.get("po_approved") == "yes",
                    waiting_category=request.form.get("waiting_category") or None,
                    waiting_reason=request.form.get("waiting_reason") or None,
                    related_event_id=request.form.get("related_event_id") or None,
                    bug_scope=request.form.get("bug_scope") or None,
                    evidence_source=request.form.get("evidence_source") or None,
                    result=request.form.get("result") or None,
                )
            except (MilestoneArtifactError, WorkflowRecordError) as exc:
                raise WebUIError("invalid_workflow_event", str(exc), status_code=400) from exc
        flash("Milestone workflow event recorded in the tracked workflow sidecar.", "success")
        return redirect(url_for("milestone_page"))

    @app.post("/milestones/handoff")
    def generate_milestone_handoff_packet():
        require_mutation_request()
        with repository_action_lock:
            require_current_repository_selection()
            submitted_repository = request.form.get("repository_key", "")
            submitted_generation = request.form.get("repository_generation", "")
            try:
                generation = int(submitted_generation)
            except ValueError as exc:
                raise WebUIError(
                    "stale_handoff_request",
                    "Refresh the milestone page and retry packet generation.",
                    status_code=409,
                ) from exc
            repository = current_repository()
            if (
                submitted_repository != repository.key
                or generation != g.repository_registry_generation
            ):
                raise WebUIError(
                    "stale_handoff_request",
                    "The selected repository changed since this page was rendered. Refresh and retry.",
                    status_code=409,
                )
            selection = load_milestone_selection(
                repository.path,
                effective_state_root,
                repository.milestone_root,
            )
            expected_id = request.form.get("milestone_id", "")
            expected_prompt_path = request.form.get("prompt_path", "")
            if (
                selection["status"] != "selected"
                or selection["selection"]["milestone_id"] != expected_id
                or selection["selection"]["prompt_path"] != expected_prompt_path
            ):
                raise WebUIError(
                    "stale_handoff_milestone",
                    "The selected milestone changed or is invalid. Refresh the page and retry.",
                    status_code=409,
                )
            packet_type = request.form.get("packet_type", "")
            try:
                packet = generate_handoff_packet(
                    repository_root=repository.path,
                    repository_name=repository.name,
                    repository_id=repository.repository_id,
                    state_root=effective_state_root,
                    milestone_root=repository.milestone_root,
                    documents=repository.documents,
                    candidate=selection["selection"],
                    packet_type=packet_type,
                )
            except (HandoffPacketError, MilestoneArtifactError, WorkflowRecordError) as exc:
                raise WebUIError("handoff_packet_failed", str(exc), status_code=400) from exc
            try:
                inventory = discover_milestones(repository.path, repository.milestone_root)
                workflow = load_milestone_workflow(
                    repository.path,
                    effective_state_root,
                    repository.milestone_root,
                    selection["selection"],
                )
            except (MilestoneArtifactError, WorkflowRecordError) as exc:
                workflow = {"status": "invalid", "reason": str(exc)}
                inventory = discover_milestones(repository.path, repository.milestone_root)
        return render_template(
            "milestones.html",
            candidates=inventory["candidates"],
            inventory_issues=inventory["issues"],
            selection=selection,
            milestone_root=repository.milestone_root,
            workflow=workflow,
            handoff_packet=packet,
            handoff_packet_type=packet_type,
        )

    @app.post("/workflow/snapshot/create")
    def workflow_create_snapshot_action():
        _require_csrf()
        raise WebUIError("legacy_workflow_disabled", "Workflow artifact creation is no longer available from the legacy Workflow surface.", status_code=410)

    @app.post("/workflow/stage/prepare")
    def workflow_prepare_stage_action():
        _require_csrf()
        raise WebUIError("legacy_workflow_disabled", "Legacy stage actions are disabled; use Git Review to select and confirm a path.", status_code=410)

    @app.get("/workflow/stage/plan")
    def workflow_stage_review_page():
        plan_id = request.args.get("plan_id", "")
        if not plan_id:
            raise WebUIError("invalid_input", "Stage plan ID is required.", status_code=400)
        plan_root = effective_state_root / current_repository().repository_id / "workflow" / "stage_plans" / plan_id
        if not plan_root.exists():
            raise WebUIError("stage_plan_not_found", "Stage plan was not found.", status_code=404)
        plan = json.loads((plan_root / "plan.json").read_text(encoding="utf-8"))
        branch = plan["branch"]["name"] if plan["branch"]["state"] == "attached" else "(detached)"
        summary = {
            "action": "Stage",
            "repository": plan["repository_root"],
            "branch": branch,
            "head_short": plan["head"][:7],
            "head_full": plan["head"],
            "candidate_count": int(plan.get("candidate_record_count", 0)),
            "candidate_paths": [item["path"] for item in plan.get("candidate_records", [])],
            "candidate_classifications": {
                item["path"]: item.get("classification", "unknown")
                for item in plan.get("candidate_records", [])
            },
            "plan_id": plan_id,
            "compatibility": "Current read-only compatibility is informational and not authoritative.",
        }
        return render_template("workflow_plan_review.html", mode="stage", plan=summary)

    @app.post("/workflow/stage/approve")
    def workflow_stage_approve_action():
        _require_csrf()
        raise WebUIError("legacy_workflow_disabled", "Legacy stage approvals are disabled; use Git Review.", status_code=410)

    @app.post("/workflow/commit/prepare")
    def workflow_prepare_commit_action():
        _require_csrf()
        raise WebUIError("legacy_workflow_disabled", "Legacy commit preparation is disabled; use Git Review to review staged content.", status_code=410)

    @app.get("/workflow/commit/plan")
    def workflow_commit_review_page():
        plan_id = request.args.get("plan_id", "")
        if not plan_id:
            raise WebUIError("invalid_input", "Commit plan ID is required.", status_code=400)
        plan_root = effective_state_root / current_repository().repository_id / "workflow" / "commit_plans" / plan_id
        if not plan_root.exists():
            raise WebUIError("commit_plan_not_found", "Commit plan was not found.", status_code=404)
        plan = json.loads((plan_root / "plan.json").read_text(encoding="utf-8"))
        branch = plan["branch"]["name"] if plan["branch"]["state"] == "attached" else "(detached)"
        summary = {
            "action": "Commit",
            "repository": plan["repository_root"],
            "branch": branch,
            "head_short": plan["head_before"][:7],
            "head_full": plan["head_before"],
            "commit_message": plan.get("commit_message", "(not available)"),
            "staged_summary": plan.get("staged_summary", []),
            "matching_snapshot_id": plan.get("matching_snapshot_id", "(no snapshot)"),
            "plan_id": plan_id,
        }
        return render_template("workflow_plan_review.html", mode="commit", plan=summary)

    @app.post("/workflow/commit/approve")
    def workflow_commit_approve_action():
        _require_csrf()
        raise WebUIError("legacy_workflow_disabled", "Legacy commit approvals are disabled; use Git Review.", status_code=410)

    return app


def run_web_server(
    *,
    repository_path: str | None = None,
    repository_registry_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
    state_root: Path | None = None,
) -> None:
    app = create_web_app(
        repository_path=repository_path,
        repository_registry_path=repository_registry_path,
        host=host,
        port=port,
        state_root=state_root,
    )
    app.run(host=host, port=port, debug=False, use_reloader=False)
