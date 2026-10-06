from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from flask import Flask, flash, redirect, render_template, request, session, url_for

from ..analysis.manager import AnalysisError, analyze_comparison
from ..compare.manager import compare_snapshots
from ..context.generator import build_and_publish_context
from ..scanner.core import DEFAULT_STATE_ROOT, run_scan_with_artifacts
from ..scanner.git_ops import ScanError, validate_git_worktree
from ..scanner.util import make_repository_id
from ..snapshot.manager import create_snapshot
from ..workflow.errors import WorkflowReasonError
from ..workflow.git_state import inspect_git_state
from ..workflow.stage_execution import execute_prepared_stage
from ..workflow.stage_plan import prepare_stage
from ..workflow.commit_execution import execute_prepared_commit
from ..workflow.commit_plan import prepare_commit
from ..workflow.status import WorkflowError, generate_milestone_status
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


def create_web_app(
    *,
    repository_path: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    state_root: Path | None = None,
) -> Flask:
    bound_host = _validate_host(host)
    if port < 1 or port > 65535:
        raise ValueError("port must be between 1 and 65535")

    repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
    repo_id = make_repository_id(repo_root)
    effective_state_root = (state_root or DEFAULT_STATE_ROOT).expanduser()

    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config["SECRET_KEY"] = secrets.token_hex(32)
    app.config["REPO_ROOT"] = repo_root
    app.config["REPO_ID"] = repo_id
    app.config["STATE_ROOT"] = effective_state_root
    app.config["BOUND_HOST"] = bound_host
    app.config["BOUND_PORT"] = port

    @app.context_processor
    def _template_context() -> dict[str, Any]:
        return {
            "csrf_token": _ensure_csrf_token(),
            "repository_name": repo_root.name,
            "repository_root": str(repo_root),
            "bound_host": bound_host,
            "bound_port": port,
        }

    def status_paths() -> tuple[Path, Path]:
        workflow_root = effective_state_root / repo_id / "workflow"
        return workflow_root / "status.json", workflow_root

    def contexts_root() -> Path:
        return effective_state_root / repo_id / "contexts"

    def snapshots_root() -> Path:
        return effective_state_root / repo_id / "snapshots"

    def comparisons_root() -> Path:
        return effective_state_root / repo_id / "comparisons"

    def analyses_root() -> Path:
        return effective_state_root / repo_id / "analyses"

    def workflow_root() -> Path:
        return effective_state_root / repo_id / "workflow"

    # Prime deterministic status on startup so dashboard can render immediately.
    generate_milestone_status(str(repo_root), state_root=effective_state_root)

    @app.errorhandler(WebUIError)
    def _handle_web_error(err: WebUIError):
        return render_template(
            "error.html",
            error_code=err.code,
            error_message=err.safe_message,
            mutation_note="NO TARGET GIT MUTATION",
        ), err.status_code

    @app.errorhandler(WorkflowReasonError)
    def _handle_workflow_reason_error(err: WorkflowReasonError):
        return render_template(
            "error.html",
            error_code=err.code,
            error_message=err.safe_message,
            mutation_note="NO TARGET GIT MUTATION",
        ), 400

    @app.errorhandler(ScanError)
    @app.errorhandler(WorkflowError)
    @app.errorhandler(AnalysisError)
    def _handle_expected_service_error(err: Exception):
        return render_template(
            "error.html",
            error_code="service_error",
            error_message=str(err),
            mutation_note="NO TARGET GIT MUTATION",
        ), 400

    @app.errorhandler(Exception)
    def _handle_unexpected_error(err: Exception):
        return render_template(
            "error.html",
            error_code="unexpected_error",
            error_message="Unexpected application error.",
            mutation_note="NO TARGET GIT MUTATION",
        ), 500

    @app.get("/")
    def dashboard():
        status_json_path, _ = status_paths()
        if not status_json_path.exists():
            generate_milestone_status(str(repo_root), state_root=effective_state_root)
        status_payload = _read_status(status_json_path)
        dashboard_view = load_dashboard_view(status_payload, repo_root)
        return render_template("dashboard.html", dashboard=dashboard_view)

    @app.post("/status/refresh")
    def refresh_status():
        _require_csrf()
        generate_milestone_status(str(repo_root), state_root=effective_state_root)
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
            current_git_state = inspect_git_state(str(repo_root))
            context_view = load_context_view(
                load_context_payload(contexts_root(), selected_context_id),
                current_git_state,
                repo_id,
                repo_root,
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
        scan_result = run_scan_with_artifacts(str(repo_root), state_root=effective_state_root)
        result = build_and_publish_context(scan_result=scan_result, query=query)
        flash("Context generated.", "success")
        return redirect(url_for("context_page", context_id=result["context_id"]))

    @app.get("/snapshots")
    def snapshots_page():
        selected_snapshot_id = request.args.get("snapshot_id")
        status_json_path, wf_root = status_paths()
        # Keep current-match cue aligned with the repository's current state.
        generate_milestone_status(str(repo_root), state_root=effective_state_root)
        status_payload = _read_status(status_json_path)
        rows = list_snapshots(
            snapshots_root(),
            repo_id,
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
        scan_result = run_scan_with_artifacts(str(repo_root), state_root=effective_state_root)
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
        generate_milestone_status(str(repo_root), state_root=effective_state_root)
        status_payload = _read_status(status_json_path)

        snapshot_rows = list_snapshots(
            snapshots_root(),
            repo_id,
            status_payload["current_snapshot_id_candidate"],
        )
        snapshots_by_id = {row.snapshot_id: row for row in snapshot_rows}
        comparisons = list_comparisons(comparisons_root(), repo_id)
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
        result = compare_snapshots(before_snapshot_id, after_snapshot_id, str(repo_root), state_root=effective_state_root)
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
        comparisons = list_comparisons(comparisons_root(), repo_id)
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
        result = analyze_comparison(comparison_id, str(repo_root), state_root=effective_state_root)
        flash("Local GPT-OSS analysis completed.", "success")
        return redirect(url_for("analysis_page", comparison_id=comparison_id, analysis_id=result["analysis_id"]))

    @app.get("/workflow")
    def workflow_page():
        status_json_path, wf_root = status_paths()
        generate_milestone_status(str(repo_root), state_root=effective_state_root)
        status_payload = _read_status(status_json_path)
        git_state = inspect_git_state(str(repo_root))
        workflow_rows = summarize_workflow_artifacts(wf_root, git_state)
        return render_template(
            "workflow.html",
            workflow_state=status_payload["workflow_state"],
            matching_snapshot_exists=status_payload["matching_snapshot_exists"],
            matching_snapshot_id=status_payload["matching_snapshot_id"],
            rows=workflow_rows,
        )

    @app.post("/workflow/snapshot/create")
    def workflow_create_snapshot_action():
        _require_csrf()
        scan_result = run_scan_with_artifacts(str(repo_root), state_root=effective_state_root)
        result = create_snapshot(scan_result=scan_result, state_root=effective_state_root)
        flash(f"Matching Snapshot ready: {result['snapshot_id']}.", "success")
        return redirect(url_for("workflow_page"))

    @app.post("/workflow/stage/prepare")
    def workflow_prepare_stage_action():
        _require_csrf()
        result = prepare_stage(str(repo_root), include_all=True, state_root=effective_state_root)
        flash("Stage plan prepared.", "success")
        return redirect(url_for("workflow_stage_review_page", plan_id=result["plan_id"]))

    @app.get("/workflow/stage/plan")
    def workflow_stage_review_page():
        plan_id = request.args.get("plan_id", "")
        if not plan_id:
            raise WebUIError("invalid_input", "Stage plan ID is required.", status_code=400)
        plan_root = effective_state_root / repo_id / "workflow" / "stage_plans" / plan_id
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
        plan_id = request.form.get("plan_id", "")
        if not plan_id:
            raise WebUIError("invalid_input", "Stage plan ID is required.", status_code=400)
        result = execute_prepared_stage(str(repo_root), plan_id, approve=True, state_root=effective_state_root)
        flash(f"Stage execution succeeded for plan {plan_id}.", "success")
        return redirect(url_for("workflow_page"))

    @app.post("/workflow/commit/prepare")
    def workflow_prepare_commit_action():
        _require_csrf()
        status_json_path, _ = status_paths()
        generate_milestone_status(str(repo_root), state_root=effective_state_root)
        status_payload = _read_status(status_json_path)
        if not status_payload["matching_snapshot_exists"]:
            raise WebUIError("matching_snapshot_required", "Create the matching Snapshot before preparing Commit.")
        message = request.form.get("commit_message", "").strip()
        if not message:
            raise WebUIError("invalid_input", "Commit message is required.", status_code=400)
        result = prepare_commit(str(repo_root), message, state_root=effective_state_root)
        flash("Commit plan prepared.", "success")
        return redirect(url_for("workflow_commit_review_page", plan_id=result["plan_id"]))

    @app.get("/workflow/commit/plan")
    def workflow_commit_review_page():
        plan_id = request.args.get("plan_id", "")
        if not plan_id:
            raise WebUIError("invalid_input", "Commit plan ID is required.", status_code=400)
        plan_root = effective_state_root / repo_id / "workflow" / "commit_plans" / plan_id
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
        plan_id = request.form.get("plan_id", "")
        if not plan_id:
            raise WebUIError("invalid_input", "Commit plan ID is required.", status_code=400)
        result = execute_prepared_commit(str(repo_root), plan_id, approve=True, state_root=effective_state_root)
        flash(f"Commit execution succeeded for plan {plan_id}.", "success")
        return redirect(url_for("workflow_page"))

    return app


def run_web_server(
    *,
    repository_path: str,
    host: str = "127.0.0.1",
    port: int = 8765,
    state_root: Path | None = None,
) -> None:
    app = create_web_app(repository_path=repository_path, host=host, port=port, state_root=state_root)
    app.run(host=host, port=port, debug=False, use_reloader=False)
