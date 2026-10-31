from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .analysis.manager import AnalysisError, analyze_comparison
from .compare.manager import compare_snapshots
from .context.generator import build_and_publish_context
from .scanner.core import run_scan, run_scan_with_artifacts
from .scanner.git_ops import ScanError
from .snapshot.manager import create_snapshot
from .web.app import run_web_server
from .workflow.commit_execution import execute_prepared_commit
from .workflow.commit_plan import prepare_commit
from .workflow.errors import WorkflowReasonError
from .workflow.stage_execution import execute_prepared_stage
from .workflow.stage_plan import prepare_stage
from .workflow.status import WorkflowError, generate_milestone_status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="repoctl", description="Repo Control Plane CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser("scan", help="Run deterministic repository scan")
    scan_parser.add_argument("repository", help="Path to target Git repository")

    context_parser = subparsers.add_parser("context", help="Generate deterministic context pack")
    context_parser.add_argument("query", help="Navigation query text")
    context_parser.add_argument("--repository", help="Path inside target Git repository")

    snapshot_parser = subparsers.add_parser("snapshot", help="Capture immutable deterministic snapshot")
    snapshot_parser.add_argument("--repository", help="Path inside target Git repository")

    compare_parser = subparsers.add_parser("compare", help="Compare two immutable snapshots")
    compare_parser.add_argument("before_snapshot_id", help="Before snapshot identifier")
    compare_parser.add_argument("after_snapshot_id", help="After snapshot identifier")
    compare_parser.add_argument("--repository", help="Path inside target Git repository")

    analyze_parser = subparsers.add_parser("analyze", help="Analyze one immutable comparison with local GPT-OSS")
    analyze_parser.add_argument("comparison_id", help="Comparison identifier")
    analyze_parser.add_argument("--repository", help="Path inside target Git repository")

    web_parser = subparsers.add_parser("web", help="Start local browser UI")
    web_repository = web_parser.add_mutually_exclusive_group(required=True)
    web_repository.add_argument("--repository", help="Path to target Git repository")
    web_repository.add_argument("--repository-registry", help="Server-side TOML registry of known repositories")
    web_parser.add_argument("--host", default="127.0.0.1", help="Bind host (M009 loopback only)")
    web_parser.add_argument("--port", default=8765, type=int, help="Bind port")

    mcp_parser = subparsers.add_parser("mcp-architect", help="Start read-only Architect MCP stdio adapter")
    mcp_parser.add_argument(
        "--repo-control-url",
        default="http://127.0.0.1:8765",
        help="Base URL for Repo Control web server (default: http://127.0.0.1:8765)",
    )

    milestone_parser = subparsers.add_parser("milestone", help="Milestone workflow commands")
    milestone_subparsers = milestone_parser.add_subparsers(dest="milestone_command", required=True)
    milestone_status_parser = milestone_subparsers.add_parser("status", help="Report deterministic read-only Git workflow state")
    milestone_status_parser.add_argument("--repository", help="Path inside target Git repository")
    milestone_prepare_commit_parser = milestone_subparsers.add_parser(
        "prepare-commit", help="Prepare immutable staged commit plan"
    )
    milestone_prepare_commit_parser.add_argument("--message", required=True, help="Exact commit message")
    milestone_prepare_commit_parser.add_argument("--repository", help="Path inside target Git repository")
    milestone_commit_parser = milestone_subparsers.add_parser("commit", help="Execute prepared immutable commit plan")
    milestone_commit_parser.add_argument("plan_id", help="Prepared immutable commit plan identifier")
    milestone_commit_parser.add_argument("--approve", action="store_true", help="Explicitly approve exact plan execution")
    milestone_commit_parser.add_argument("--repository", help="Path inside target Git repository")
    milestone_prepare_stage_parser = milestone_subparsers.add_parser(
        "prepare-stage", help="Prepare immutable exact staging plan"
    )
    milestone_prepare_stage_parser.add_argument("--all", action="store_true", required=True, help="Stage all currently visible eligible changes from the reviewed plan")
    milestone_prepare_stage_parser.add_argument("--repository", help="Path inside target Git repository")
    milestone_stage_parser = milestone_subparsers.add_parser("stage", help="Execute prepared immutable staging plan")
    milestone_stage_parser.add_argument("plan_id", help="Prepared immutable stage plan identifier")
    milestone_stage_parser.add_argument("--approve", action="store_true", help="Explicitly approve exact stage plan execution")
    milestone_stage_parser.add_argument("--repository", help="Path inside target Git repository")

    coder_context_parser = subparsers.add_parser(
        "coder-context", help="Retrieve authoritative active milestone context for Coder agents"
    )
    coder_context_parser.add_argument("--repository", help="Path inside target Git repository")
    coder_context_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format (default: text)"
    )
    coder_context_parser.add_argument("--state-root", help="Explicit state root override")
    coder_context_parser.add_argument(
        "--milestone-root", help="Compatibility assertion; must match the centrally configured milestone root"
    )

    bootstrap_preview_parser = subparsers.add_parser(
        "coder-bootstrap-preview",
        help="Render read-only per-repository Copilot and Codex hook registration previews",
    )
    bootstrap_preview_parser.add_argument("--repository-key", required=True)
    bootstrap_preview_parser.add_argument("--repository-id", required=True)
    bootstrap_preview_parser.add_argument("--repository-root", required=True)
    bootstrap_preview_parser.add_argument(
        "--python-executable",
        default=None,
        help="Trusted central Repo Control II Python interpreter (defaults to its project virtualenv)",
    )

    coder_status_parser = subparsers.add_parser(
        "coder-status", help="Report active milestone lifecycle and workflow status for Coder agents"
    )
    coder_status_parser.add_argument("--repository", help="Path inside target Git repository")
    coder_status_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format (default: text)"
    )
    coder_status_parser.add_argument("--state-root", help="Explicit state root override")
    coder_status_parser.add_argument(
        "--milestone-root", help="Compatibility assertion; must match the centrally configured milestone root"
    )

    procedure_status_parser = subparsers.add_parser(
        "coder-procedure-status", help="Inspect shared Coder procedure release and generated-file drift"
    )
    procedure_status_parser.add_argument("--repository", help="Path inside target managed Git repository")
    procedure_status_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format (default: text)"
    )

    procedure_preview_parser = subparsers.add_parser(
        "coder-procedure-preview", help="Preview shared procedure reconciliation without writing repository files"
    )
    procedure_preview_parser.add_argument("--repository", help="Path inside target managed Git repository")
    procedure_preview_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format (default: text)"
    )

    from .intelligence.service import DEFAULT_BOUNDS, OPERATIONS
    from .intelligence.measurements import RATINGS
    intelligence_parser = subparsers.add_parser(
        "coder-intelligence", help="Targeted static structural evidence for the server-selected allowlisted repository"
    )
    intelligence_parser.add_argument("operation", choices=OPERATIONS, nargs="?", default="stats")
    intelligence_parser.add_argument("query", nargs="?", default="")
    intelligence_parser.add_argument("--target", default="")
    intelligence_parser.add_argument("--direction", choices=["incoming", "outgoing", "both"], default="both")
    intelligence_parser.add_argument("--relation", action="append", dest="relations", default=[])
    for key, default in DEFAULT_BOUNDS.items():
        intelligence_parser.add_argument("--" + key.replace("_", "-"), type=int, default=default)
    rating_parser = subparsers.add_parser("coder-intelligence-rate", help="Optional usefulness/inspection feedback for a completed use")
    rating_parser.add_argument("use_id")
    rating_parser.add_argument("rating", choices=RATINGS)
    rating_parser.add_argument("--inspected", choices=["yes", "no"])
    subparsers.add_parser(
        "coder-intelligence-refresh",
        help="Refresh the fixed configured graph for this authorized managed Coder workspace",
    )

    coder_question_parser = subparsers.add_parser(
        "coder-question", help="Submit a bounded question or blocker from the Coder"
    )
    coder_question_parser.add_argument("summary", help="Question summary or description")
    coder_question_parser.add_argument(
        "--blocking", action="store_true", help="Mark question as a blocker that pauses implementation into WAITING"
    )
    coder_question_parser.add_argument(
        "--category",
        choices=["clarification", "decision", "blocker", "other"],
        help="Question category (default: blocker if --blocking, else None)",
    )
    coder_question_parser.add_argument("--repository", help="Path inside target Git repository")
    coder_question_parser.add_argument("--state-root", help="Explicit state root override")
    coder_question_parser.add_argument(
        "--milestone-root", help="Compatibility assertion; must match the centrally configured milestone root"
    )

    coder_bug_parser = subparsers.add_parser(
        "coder-bug", help="Record discovery of a defect during implementation"
    )
    coder_bug_parser.add_argument("summary", help="Defect discovery summary")
    coder_bug_parser.add_argument("--repository", help="Path inside target Git repository")
    coder_bug_parser.add_argument("--state-root", help="Explicit state root override")
    coder_bug_parser.add_argument(
        "--milestone-root", help="Compatibility assertion; must match the centrally configured milestone root"
    )

    coder_test_parser = subparsers.add_parser(
        "coder-test", help="Record test execution evidence from the Coder"
    )
    coder_test_parser.add_argument("summary", help="Test execution summary")
    coder_test_parser.add_argument("--source", required=True, help="Test evidence source (e.g. command or suite)")
    coder_test_parser.add_argument("--result", required=True, help="Test outcome result (e.g. pass/fail counts)")
    coder_test_parser.add_argument("--repository", help="Path inside target Git repository")
    coder_test_parser.add_argument("--state-root", help="Explicit state root override")
    coder_test_parser.add_argument(
        "--milestone-root", help="Compatibility assertion; must match the centrally configured milestone root"
    )

    coder_closeout_parser = subparsers.add_parser(
        "coder-closeout", help="Submit milestone closeout artifact for Product Owner review"
    )
    coder_closeout_parser.add_argument(
        "--summary", default="Closeout submitted for review", help="Closeout submission summary"
    )
    coder_closeout_parser.add_argument("--repository", help="Path inside target Git repository")
    coder_closeout_parser.add_argument("--state-root", help="Explicit state root override")
    coder_closeout_parser.add_argument(
        "--milestone-root", help="Compatibility assertion; must match the centrally configured milestone root"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "coder-bootstrap-preview":
        from .bootstrap_preview import TRUSTED_PYTHON, build_bootstrap_preview, format_bootstrap_preview

        try:
            preview = build_bootstrap_preview(
                repository_key=args.repository_key,
                repository_id=args.repository_id,
                repository_root=args.repository_root,
                python_executable=args.python_executable or TRUSTED_PYTHON,
            )
        except ValueError as exc:
            print(f"bootstrap preview failed: {exc}", file=sys.stderr)
            return 2
        print(format_bootstrap_preview(preview), end="")
        return 0

    if args.command == "scan":
        try:
            result = run_scan(args.repository)
        except ScanError as exc:
            print(f"scan failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"scan failed: {exc}", file=sys.stderr)
            return 1

        branch_text = result["branch"]["name"] if result["branch"]["state"] == "attached" else "detached"
        print("scan complete")
        print(f"target: {result['repository_root']}")
        print(f"repository_id: {result['repository_id']}")
        print(f"branch: {branch_text}")
        print(f"head: {result['head_commit']}")
        print(f"output: {result['output_dir']}")
        print(f"parse_errors: {result['parse_error_count']}")
        return 0

    if args.command == "context":
        repository_path = args.repository if args.repository else str(Path.cwd())
        try:
            scan_result = run_scan_with_artifacts(repository_path)
            context_result = build_and_publish_context(scan_result=scan_result, query=args.query)
        except ValueError as exc:
            print(f"context failed: {exc}", file=sys.stderr)
            return 2
        except ScanError as exc:
            print(f"context failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"context failed: {exc}", file=sys.stderr)
            return 1

        print("context complete")
        print(f"target: {context_result['repository_root']}")
        print(f"repository_id: {context_result['repository_id']}")
        print(f"context_id: {context_result['context_id']}")
        print(f"output: {context_result['context_dir']}")
        print(f"match_status: {context_result['match_status']}")
        print(f"seed_count: {context_result['seed_count']}")
        print(f"selected_file_count: {context_result['selected_file_count']}")
        print(f"selected_symbol_count: {context_result['selected_symbol_count']}")
        print(f"selected_relationship_count: {context_result['selected_relationship_count']}")
        print(f"selected_test_reference_count: {context_result['selected_test_reference_count']}")
        return 0

    if args.command == "snapshot":
        repository_path = args.repository if args.repository else str(Path.cwd())
        try:
            scan_result = run_scan_with_artifacts(repository_path)
            snapshot_result = create_snapshot(scan_result=scan_result)
        except ScanError as exc:
            print(f"snapshot failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"snapshot failed: {exc}", file=sys.stderr)
            return 1

        print("snapshot complete")
        print(f"target: {snapshot_result['repository_root']}")
        print(f"repository_id: {snapshot_result['repository_id']}")
        print(f"snapshot_id: {snapshot_result['snapshot_id']}")
        print(f"output: {snapshot_result['snapshot_dir']}")
        print(f"reused_existing: {snapshot_result['reused_existing']}")
        return 0

    if args.command == "compare":
        repository_path = args.repository if args.repository else str(Path.cwd())
        try:
            compare_result = compare_snapshots(args.before_snapshot_id, args.after_snapshot_id, repository_path)
        except ScanError as exc:
            print(f"compare failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"compare failed: {exc}", file=sys.stderr)
            return 1

        print("compare complete")
        print(f"target: {compare_result['repository_root']}")
        print(f"repository_id: {compare_result['repository_id']}")
        print(f"comparison_id: {compare_result['comparison_id']}")
        print(f"output: {compare_result['comparison_dir']}")
        print(f"reused_existing: {compare_result['reused_existing']}")
        return 0

    if args.command == "analyze":
        repository_path = args.repository if args.repository else str(Path.cwd())
        try:
            analysis_result = analyze_comparison(args.comparison_id, repository_path)
        except AnalysisError as exc:
            msg = str(exc)
            if msg.startswith("provider error ["):
                print(msg, file=sys.stderr)
            else:
                print(f"analyze failed: {msg}", file=sys.stderr)
            return 2
        except ScanError as exc:
            print(f"analyze failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"analyze failed: {exc}", file=sys.stderr)
            return 1

        print("analyze complete")
        print(f"target: {analysis_result['repository_root']}")
        print(f"repository_id: {analysis_result['repository_id']}")
        print(f"comparison_id: {analysis_result['comparison_id']}")
        print(f"packet_id: {analysis_result['packet_id']}")
        print(f"request_id: {analysis_result['request_id']}")
        print(f"analysis_id: {analysis_result['analysis_id']}")
        print(f"provider: {analysis_result['provider']}")
        print(f"model_name: {analysis_result['model_name']}")
        print(f"model_digest: {analysis_result['model_digest']}")
        print(f"output: {analysis_result['analysis_dir']}")
        print(f"reused_existing: {analysis_result['reused_existing']}")
        return 0

    if args.command == "web":
        try:
            run_web_server(
                repository_path=args.repository,
                repository_registry_path=args.repository_registry,
                host=args.host,
                port=args.port,
            )
        except ValueError as exc:
            print(f"web failed: {exc}", file=sys.stderr)
            return 2
        except ScanError as exc:
            print(f"web failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"web failed: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.command == "mcp-architect":
        from .mcp.architect_adapter import serve_stdio

        try:
            serve_stdio(repo_control_url=args.repo_control_url)
            return 0
        except Exception as exc:
            print(f"mcp-architect failed: {exc}", file=sys.stderr)
            return 1

    if args.command == "milestone" and args.milestone_command == "status":
        repository_path = args.repository if args.repository else str(Path.cwd())
        try:
            status_result = generate_milestone_status(repository_path)
        except WorkflowError as exc:
            print(f"milestone status failed: {exc}", file=sys.stderr)
            return 2
        except ScanError as exc:
            print(f"milestone status failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"milestone status failed: {exc}", file=sys.stderr)
            return 1

        branch_text = (
            status_result["branch"]["name"]
            if status_result["branch"]["state"] == "attached"
            else "(detached)"
        )
        upstream_ref = status_result["upstream"]["ref"] if status_result["upstream"]["ref"] else "none"
        relation = status_result["upstream"]["relation"] if status_result["upstream"]["relation"] else "unavailable"
        ahead = status_result["upstream"]["ahead"] if status_result["upstream"]["ahead"] is not None else "unavailable"
        behind = status_result["upstream"]["behind"] if status_result["upstream"]["behind"] is not None else "unavailable"
        operations = ", ".join(status_result["git_operations"]) if status_result["git_operations"] else "none"
        matching_snapshot = status_result["matching_snapshot_id"] if status_result["matching_snapshot_id"] else "none"

        print("milestone status complete")
        print(f"Repository: {status_result['repository_root']}")
        print(f"Branch: {branch_text}")
        print(f"HEAD: {status_result['head']}")
        print(f"Workflow state: {status_result['workflow_state']}")
        print(f"Staged: {status_result['working_tree']['staged']['count']}")
        print(f"Unstaged: {status_result['working_tree']['unstaged']['count']}")
        print(f"Untracked: {status_result['working_tree']['untracked']['count']}")
        print(f"Unmerged: {status_result['working_tree']['unmerged']['count']}")
        print(f"Upstream: {upstream_ref}")
        print(f"Local upstream relation: {relation}")
        print(f"Ahead: {ahead}")
        print(f"Behind: {behind}")
        print("Remote refresh: not performed")
        print(f"Git operation: {operations}")
        print(f"Matching snapshot: {matching_snapshot}")
        print(f"status_json: {status_result['status_json']}")
        print(f"status_md: {status_result['status_md']}")
        return 0

    if args.command == "milestone" and args.milestone_command == "prepare-commit":
        repository_path = args.repository if args.repository else str(Path.cwd())
        try:
            result = prepare_commit(repository_path=repository_path, commit_message=args.message)
        except WorkflowReasonError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except WorkflowError as exc:
            print(f"milestone prepare-commit failed: {exc}", file=sys.stderr)
            return 2
        except ScanError as exc:
            print(f"milestone prepare-commit failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"milestone prepare-commit failed: {exc}", file=sys.stderr)
            return 1

        branch_text = (
            result["branch"]["name"] if result["branch"]["state"] == "attached" else "(detached)"
        )
        print("prepared commit")
        print(f"Repository: {result['repository_root']}")
        print(f"Branch: {branch_text}")
        print(f"HEAD: {result['head_before']}")
        print("Snapshot:")
        print(f"{result['matching_snapshot_id']}")
        print(f"Staged files: {result['staged_record_count']}")
        for item in result["staged_summary"]:
            print(f"  {item['status']} {item['path']}")
        print(f"Unstaged: {result['unstaged_count']}")
        print(f"Untracked: {result['untracked_count']}")
        print(f"Conflicts: {result['unmerged_count']}")
        print(f"Git op: {'active' if result['git_operation_in_progress'] else 'none'}")
        print("Commit message:")
        print("")
        for line in result["commit_message"].splitlines() or [""]:
            print(f"  {line}")
        print("")
        print(f"Plan: {result['plan_id']}")
        print("NO GIT MUTATION PERFORMED")
        print("To explicitly approve this exact plan:")
        print(f"  repoctl milestone commit {result['plan_id']} --approve")
        print(f"plan_dir: {result['plan_dir']}")
        print(f"reused_existing: {result['reused_existing']}")
        return 0

    if args.command == "milestone" and args.milestone_command == "commit":
        repository_path = args.repository if args.repository else str(Path.cwd())
        try:
            result = execute_prepared_commit(
                repository_path=repository_path,
                plan_id=args.plan_id,
                approve=args.approve,
            )
        except WorkflowReasonError as exc:
            print(str(exc), file=sys.stderr)
            if exc.code == "commit_succeeded_audit_failed" and exc.commit_id:
                print(f"resulting_commit_id: {exc.commit_id}", file=sys.stderr)
            if exc.code == "post_commit_verification_failed" and exc.commit_id:
                print(f"resulting_commit_id: {exc.commit_id}", file=sys.stderr)
            return 2
        except WorkflowError as exc:
            print(f"milestone commit failed: {exc}", file=sys.stderr)
            return 2
        except ScanError as exc:
            print(f"milestone commit failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"milestone commit failed: {exc}", file=sys.stderr)
            return 1

        print("milestone commit complete")
        print(f"Repository: {result['repository_root']}")
        print(f"Plan: {result['plan_id']}")
        print(f"Execution: {result['execution_id']}")
        print(f"Branch: {result['branch']}")
        print(f"HEAD before: {result['head_before']}")
        print(f"HEAD after: {result['head_after']}")
        print("Remote refresh: not performed")
        print("Push performed: false")
        print(f"execution_dir: {result['execution_dir']}")
        print(f"reused_existing: {result['reused_existing']}")
        return 0

    if args.command == "milestone" and args.milestone_command == "prepare-stage":
        repository_path = args.repository if args.repository else str(Path.cwd())
        try:
            result = prepare_stage(repository_path=repository_path, include_all=args.all)
        except WorkflowReasonError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except WorkflowError as exc:
            print(f"milestone prepare-stage failed: {exc}", file=sys.stderr)
            return 2
        except ScanError as exc:
            print(f"milestone prepare-stage failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"milestone prepare-stage failed: {exc}", file=sys.stderr)
            return 1

        branch_text = result["branch"]["name"] if result["branch"]["state"] == "attached" else "(detached)"
        print("prepared staging plan")
        print(f"Repository: {result['repository_root']}")
        print(f"Branch: {branch_text}")
        print(f"HEAD: {result['head']}")
        print(f"Changes proposed for staging: {result['candidate_record_count']}")
        for item in result["candidate_summary"]:
            print(f"  {item['status']} {item['path']}")
        print(f"Existing staged changes: {result['staged_count']}")
        print(f"Conflicts: {result['unmerged_count']}")
        print(f"Git operation: {'active' if result['git_operation_in_progress'] else 'none'}")
        print(f"Plan: {result['plan_id']}")
        print("NO TARGET GIT MUTATION PERFORMED")
        print("To approve this exact plan:")
        print(f"  repoctl milestone stage {result['plan_id']} --approve")
        print(f"plan_dir: {result['plan_dir']}")
        print(f"reused_existing: {result['reused_existing']}")
        return 0

    if args.command == "milestone" and args.milestone_command == "stage":
        repository_path = args.repository if args.repository else str(Path.cwd())
        try:
            result = execute_prepared_stage(repository_path=repository_path, plan_id=args.plan_id, approve=args.approve)
        except WorkflowReasonError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        except WorkflowError as exc:
            print(f"milestone stage failed: {exc}", file=sys.stderr)
            return 2
        except ScanError as exc:
            print(f"milestone stage failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"milestone stage failed: {exc}", file=sys.stderr)
            return 1

        print("milestone stage complete")
        print(f"Repository: {result['repository_root']}")
        print(f"Plan: {result['plan_id']}")
        print(f"Execution: {result['execution_id']}")
        print(f"Branch: {result['branch']}")
        print(f"HEAD before: {result['head_before']}")
        print(f"HEAD after: {result['head_after']}")
        print(f"Staged paths: {result['candidate_record_count']}")
        for item in result["staged_summary"]:
            print(f"  {item['status']} {item['path']}")
        print("Remote refresh: not performed")
        print("Commit performed: false")
        print("Push performed: false")
        print(f"execution_dir: {result['execution_dir']}")
        print(f"reused_existing: {result['reused_existing']}")
        return 0

    if args.command in {"coder-intelligence", "coder-intelligence-rate"}:
        from .intelligence import client
        from .intelligence.provider import IntelligenceError
        from .intelligence.service import QUERY_PROPERTIES, encoded
        try:
            if args.command == "coder-intelligence":
                result = client.query({key: getattr(args, key) for key in QUERY_PROPERTIES})
            else:
                result = client.rate(args.use_id, args.rating, None if args.inspected is None else args.inspected == "yes")
        except IntelligenceError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(encoded(result).decode("ascii"))
        return 0 if result.get("outcome", "OK") in {"OK", "TRUNCATED"} else 2

    if args.command == "coder-intelligence-refresh":
        from .intelligence import client
        from .intelligence.provider import IntelligenceError
        from .intelligence.service import encoded
        from .workflow.coder_interface import (
            CoderInterfaceError,
            get_coder_status,
            resolve_repository_entry,
        )

        try:
            entry = resolve_repository_entry()
            status = get_coder_status(repository_path=entry.path)
            workflow = status["workflow"]
            if (
                status["repository"]["repository_id"] != entry.repository_id
                or status["repository"]["key"] != entry.key
                or status["milestone"]["phase"] != "IN_PROGRESS"
                or not status["authorization"]["authorized"]
                or workflow.get("waiting_category") is not None
                or workflow.get("waiting_reason") is not None
            ):
                raise CoderInterfaceError(
                    "Intelligence refresh requires this managed workspace's authorized IN_PROGRESS milestone "
                    "with no active blocking condition."
                )
            result = client.refresh(
                workspace_repository_key=entry.key,
                workspace_repository_id=entry.repository_id,
                milestone_id=status["milestone"]["id"],
                prompt_path=status["prompt"]["path"],
                prompt_identity=status["prompt"]["identity"],
                event_count=workflow["event_count"],
            )
        except (CoderInterfaceError, IntelligenceError) as exc:
            print(f"coder intelligence refresh failed: {exc}", file=sys.stderr)
            return 2
        print(encoded(result).decode("ascii"))
        return 0 if result.get("outcome") in {"CURRENT", "REFRESHED", "NOT_AVAILABLE"} else 2

    if args.command == "coder-context":
        from .workflow.coder_interface import (
            CoderInterfaceError,
            format_coder_context_text,
            get_coder_context,
        )

        try:
            context_data = get_coder_context(
                repository_path=args.repository,
                state_root=args.state_root,
                milestone_root=args.milestone_root,
            )
        except CoderInterfaceError as exc:
            print(f"coder context failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"coder context failed: {exc}", file=sys.stderr)
            return 1

        if args.format == "json":
            print(json.dumps(context_data, ensure_ascii=False, indent=2))
        else:
            print(format_coder_context_text(context_data))
        return 0

    if args.command == "coder-status":
        from .workflow.coder_interface import (
            CoderInterfaceError,
            format_coder_status_text,
            get_coder_status,
        )

        try:
            status_data = get_coder_status(
                repository_path=args.repository,
                state_root=args.state_root,
                milestone_root=args.milestone_root,
            )
        except CoderInterfaceError as exc:
            print(f"coder status failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"coder status failed: {exc}", file=sys.stderr)
            return 1

        if args.format == "json":
            print(json.dumps(status_data, ensure_ascii=False, indent=2))
        else:
            print(format_coder_status_text(status_data))
        return 0

    if args.command in {"coder-procedure-status", "coder-procedure-preview"}:
        from .procedure_governance import (
            ProcedureGovernanceError,
            format_procedure_preview_text,
            format_procedure_status_text,
            inspect_procedure,
            load_bundle,
            preview_reconciliation,
        )
        from .workflow.coder_interface import CoderInterfaceError, resolve_repository_entry

        try:
            entry = resolve_repository_entry(repository_path=args.repository)
            bundle = load_bundle()
            if args.command == "coder-procedure-status":
                result = inspect_procedure(entry, bundle)
                text = format_procedure_status_text(result)
            else:
                plan = preview_reconciliation(entry, bundle)
                result = plan.as_dict(include_diffs=True)
                text = format_procedure_preview_text(plan)
        except (CoderInterfaceError, ProcedureGovernanceError, OSError, UnicodeError) as exc:
            print(f"coder procedure command failed: {exc}", file=sys.stderr)
            return 2

        if args.format == "json":
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(text)
        return 0

    if args.command == "coder-question":
        from .workflow.coder_interface import (
            CoderInterfaceError,
            submit_coder_question,
        )

        try:
            result = submit_coder_question(
                summary=args.summary,
                blocking=args.blocking,
                category=args.category,
                repository_path=args.repository,
                state_root=args.state_root,
                milestone_root=args.milestone_root,
            )
        except CoderInterfaceError as exc:
            print(f"coder question failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"coder question failed: {exc}", file=sys.stderr)
            return 1

        print("Question recorded successfully")
        print(f"event_id:     {result['event_id']}")
        print(f"milestone_id: {result['milestone_id']}")
        print(f"lifecycle:    {result['phase_before']} -> {result['phase_after']}")
        print(f"blocking:     {'yes' if result['blocking'] else 'no'}")
        if result["blocking"]:
            print(f"category:     {result['waiting_category']}")
            print(f"reason:       {result['waiting_reason']}")
        print(f"next_action:  {result['next_action']}")
        return 0

    if args.command == "coder-bug":
        from .workflow.coder_interface import (
            CoderInterfaceError,
            submit_coder_bug,
        )

        try:
            result = submit_coder_bug(
                summary=args.summary,
                repository_path=args.repository,
                state_root=args.state_root,
                milestone_root=args.milestone_root,
            )
        except CoderInterfaceError as exc:
            print(f"coder bug failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"coder bug failed: {exc}", file=sys.stderr)
            return 1

        print("Bug recorded successfully")
        print(f"event_id:     {result['event_id']}")
        print(f"milestone_id: {result['milestone_id']}")
        print(f"lifecycle:    {result['phase']}")
        print(f"summary:      {result['summary']}")
        print(f"next_action:  {result['next_action']}")
        return 0

    if args.command == "coder-test":
        from .workflow.coder_interface import (
            CoderInterfaceError,
            submit_coder_test_result,
        )

        try:
            result = submit_coder_test_result(
                summary=args.summary,
                source=args.source,
                result=args.result,
                repository_path=args.repository,
                state_root=args.state_root,
                milestone_root=args.milestone_root,
            )
        except CoderInterfaceError as exc:
            print(f"coder test failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"coder test failed: {exc}", file=sys.stderr)
            return 1

        print("Test result recorded successfully")
        print(f"event_id:     {result['event_id']}")
        print(f"milestone_id: {result['milestone_id']}")
        print(f"lifecycle:    {result['phase']}")
        print(f"source:       {result['evidence_source']}")
        print(f"result:       {result['result']} (reported evidence)")
        print(f"next_action:  {result['next_action']}")
        return 0

    if args.command == "coder-closeout":
        from .workflow.coder_interface import (
            CoderInterfaceError,
            submit_coder_closeout,
        )

        try:
            result = submit_coder_closeout(
                summary=args.summary,
                repository_path=args.repository,
                state_root=args.state_root,
                milestone_root=args.milestone_root,
            )
        except CoderInterfaceError as exc:
            print(f"coder closeout failed: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"coder closeout failed: {exc}", file=sys.stderr)
            return 1

        print("Closeout submitted successfully")
        print(f"event_id:      {result['event_id']}")
        print(f"milestone_id:  {result['milestone_id']}")
        print(f"lifecycle:     {result['phase_before']} -> {result['phase_after']}")
        print(f"evidence_path: {result['evidence_path']}")
        print(f"next_action:   {result['next_action']}")
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
