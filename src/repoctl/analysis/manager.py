from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from ..compare.manager import _verify_existing_comparison
from ..scanner.core import DEFAULT_STATE_ROOT
from ..scanner.git_ops import validate_git_worktree
from ..scanner.util import make_repository_id, write_json_deterministic
from .contracts import (
    MODEL_NAME,
    MODEL_PROVIDER,
    PROMPT_CONTRACT_VERSION,
    canonical_json_bytes,
    derive_analysis_id,
    derive_request_id,
)
from .packet import PacketBuildError, build_analysis_packet
from .provider import AnalysisProvider, ModelIdentity, OllamaLocalProvider, ProviderError
from .schema import ResponseValidationError, validate_and_normalize_response

ANALYSIS_REQUIRED_FILES = {"analysis_input.json", "analysis.json", "analysis.md"}


class AnalysisError(RuntimeError):
    pass


def _classify_validation_failure(message: str) -> str:
    grounding_markers = [
        "unknown evidence id",
        "duplicate evidence id",
        "zero-delta",
        "non-zero comparison response must include at least one review signal",
    ]
    if any(marker in message for marker in grounding_markers):
        return "evidence_grounding_validation_failed"
    return "structured_content_validation_failed"


def create_ollama_provider() -> AnalysisProvider:
    return OllamaLocalProvider()


def _analysis_input_sha256(analysis_input: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json_bytes(analysis_input)).hexdigest()


def _verify_existing_analysis(analysis_dir: Path) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    names = {child.name for child in analysis_dir.iterdir()}
    if names != ANALYSIS_REQUIRED_FILES:
        raise AnalysisError("analysis artifact set is incomplete or contains unexpected files")
    analysis_input = json.loads((analysis_dir / "analysis_input.json").read_text(encoding="utf-8"))
    analysis_json = json.loads((analysis_dir / "analysis.json").read_text(encoding="utf-8"))
    analysis_md = (analysis_dir / "analysis.md").read_bytes()
    if analysis_json.get("schema_version") != 1:
        raise AnalysisError("unsupported analysis schema version")
    return analysis_input, analysis_json, analysis_md


def _render_analysis_markdown(analysis_json: dict[str, Any]) -> str:
    output = analysis_json["validated_model_output"]
    trunc = analysis_json["packet_truncation_metadata"]

    lines: list[str] = []
    lines.append("# Local Structural Analysis")
    lines.append("")

    lines.append("## Authority and Provenance")
    lines.append("Deterministic Repo Control Plane comparison evidence is authoritative.")
    lines.append("GPT-OSS analysis is advisory and must be verified against source/tests before decisions or code changes.")
    lines.append(f"- analysis_id: {analysis_json['analysis_id']}")
    lines.append(f"- request_id: {analysis_json['request_id']}")
    lines.append(f"- packet_id: {analysis_json['packet_id']}")
    lines.append(f"- repository_id: {analysis_json['repository_id']}")
    lines.append(f"- comparison_id: {analysis_json['comparison_id']}")
    lines.append(f"- provider: {analysis_json['provider']}")
    lines.append(f"- model_name: {analysis_json['model_name']}")
    lines.append(f"- model_digest: {analysis_json['model_digest']}")
    lines.append(f"- prompt_contract_version: {analysis_json['prompt_contract_version']}")
    lines.append("")

    lines.append("## Deterministic Change Summary")
    lines.append(f"- structural_delta_present: {analysis_json['structural_delta_present']}")
    lines.append(f"- summary: {output['summary']}")
    lines.append(f"- summary_evidence_ids: {', '.join(output['summary_evidence_ids'])}")
    lines.append("")

    lines.append("## AI Review Signals")
    if not output["review_signals"]:
        lines.append("- none")
    else:
        for signal in output["review_signals"]:
            lines.append(f"- priority={signal['review_priority']} category={signal['category']}")
            lines.append(f"  - observation: {signal['observation']}")
            lines.append(f"  - interpretation: {signal['interpretation']}")
            lines.append(f"  - evidence_ids: {', '.join(signal['evidence_ids'])}")
    lines.append("")

    lines.append("## Questions for Human Review")
    if not output["questions_for_human_review"]:
        lines.append("- none")
    else:
        for question in output["questions_for_human_review"]:
            lines.append(f"- priority={question['review_priority']} question={question['question']}")
            lines.append(f"  - evidence_ids: {', '.join(question['evidence_ids'])}")
    lines.append("")

    lines.append("## Input Coverage and Truncation")
    lines.append(f"- category_truncation_occurred: {trunc['category_truncation_occurred']}")
    lines.append(f"- byte_truncation_occurred: {trunc['byte_truncation_occurred']}")
    lines.append(f"- final_serialized_packet_byte_count: {trunc['final_serialized_packet_byte_count']}")
    lines.append(f"- final_records_sent_by_category: {json.dumps(trunc['final_records_sent_by_category'], sort_keys=True)}")
    lines.append("")

    lines.append("## Analysis Limitations")
    lines.append("- analysis was limited to the supplied bounded structural packet")
    lines.append("- GPT-OSS did not receive source-code bodies")
    lines.append("- retained repository details may exist outside the packet")
    lines.append("- packet truncation may limit interpretation when applicable")
    lines.append("- static relationships are not runtime call traces")
    lines.append("- test references do not prove semantic coverage")
    lines.append("- AI observations are non-authoritative")
    lines.append("")

    return "\n".join(lines) + "\n"


def _build_analysis_payload(
    *,
    analysis_id: str,
    request_id: str,
    packet_payload: dict[str, Any],
    model_identity: ModelIdentity,
    normalized_model_output: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "analysis_id": analysis_id,
        "request_id": request_id,
        "packet_id": packet_payload["packet_id"],
        "repository_id": packet_payload["repository_id"],
        "comparison_id": packet_payload["comparison_id"],
        "before_snapshot_id": packet_payload["before_snapshot_id"],
        "after_snapshot_id": packet_payload["after_snapshot_id"],
        "authority": "advisory_ai",
        "provider": model_identity.provider,
        "model_name": model_identity.model_name,
        "model_digest": model_identity.model_digest,
        "prompt_contract_version": PROMPT_CONTRACT_VERSION,
        "analysis_input_sha256": _analysis_input_sha256(packet_payload),
        "structural_delta_present": packet_payload["structural_delta_present"],
        "aggregate_counts": packet_payload["aggregate_counts"],
        "packet_truncation_metadata": packet_payload["truncation_metadata"],
        "validated_model_output": normalized_model_output,
    }


def _publish_analysis(
    *,
    root: Path,
    repository_id: str,
    comparison_id: str,
    analysis_id: str,
    analysis_input: dict[str, Any],
    analysis_json: dict[str, Any],
    analysis_md: str,
) -> tuple[str, bool]:
    analyses_root = root / repository_id / "analyses" / comparison_id
    analyses_root.mkdir(parents=True, exist_ok=True)

    final_dir = analyses_root / analysis_id
    if final_dir.exists():
        existing_input, existing_json, existing_md = _verify_existing_analysis(final_dir)
        if existing_input != analysis_input or existing_json != analysis_json or existing_md != analysis_md.encode("utf-8"):
            raise AnalysisError("existing analysis content mismatch for identical analysis id")
        return str(final_dir), True

    temp_dir = Path(mkdtemp(prefix="analysis-tmp-", dir=str(analyses_root)))
    try:
        write_json_deterministic(temp_dir / "analysis_input.json", analysis_input)
        write_json_deterministic(temp_dir / "analysis.json", analysis_json)
        (temp_dir / "analysis.md").write_text(analysis_md, encoding="utf-8", newline="\n")
        temp_dir.rename(final_dir)
        return str(final_dir), False
    except Exception:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        raise


def analyze_comparison(
    comparison_id: str,
    repository_path: str,
    *,
    provider: AnalysisProvider | None = None,
    state_root: Path | None = None,
) -> dict[str, Any]:
    repo_root = validate_git_worktree(Path(repository_path).expanduser().resolve())
    repository_id = make_repository_id(repo_root)

    root = (state_root or DEFAULT_STATE_ROOT).expanduser()
    comparison_dir = root / repository_id / "comparisons" / comparison_id
    if not comparison_dir.exists():
        raise AnalysisError("comparison does not exist")

    comparison_json, _comparison_md = _verify_existing_comparison(comparison_dir)
    if comparison_json.get("repository_id") != repository_id:
        raise AnalysisError("comparison repository id mismatch")
    if comparison_json.get("comparison_id") != comparison_id:
        raise AnalysisError("comparison id mismatch")

    try:
        packet_payload = build_analysis_packet(comparison_json)
    except PacketBuildError as exc:
        raise AnalysisError(f"analysis packet build failed: {exc}") from exc

    selected_provider = provider or create_ollama_provider()
    try:
        model_identity = selected_provider.resolve_model_identity()
    except ProviderError as exc:
        raise AnalysisError(str(exc)) from exc

    if model_identity.provider != MODEL_PROVIDER:
        raise AnalysisError("unexpected model provider")
    if model_identity.model_name != MODEL_NAME:
        raise AnalysisError("unexpected model name")

    request_id = derive_request_id(
        packet_payload["packet_id"],
        model_identity.provider,
        model_identity.model_name,
        model_identity.model_digest,
        PROMPT_CONTRACT_VERSION,
    )

    try:
        model_output = selected_provider.generate_analysis(
            model_identity=model_identity,
            packet_payload=packet_payload,
            request_id=request_id,
            prompt_contract_version=PROMPT_CONTRACT_VERSION,
        )
    except ProviderError as exc:
        raise AnalysisError(str(exc)) from exc

    evidence_ids = [row["evidence_id"] for row in packet_payload["evidence_records"]]
    try:
        normalized_output = validate_and_normalize_response(
            response_payload=model_output,
            valid_evidence_ids=evidence_ids,
            structural_delta_present=packet_payload["structural_delta_present"],
        )
    except ResponseValidationError as exc:
        safe_message = str(exc).split("\n", 1)[0]
        code = _classify_validation_failure(safe_message)
        raise AnalysisError(f"provider error [{code}]: {safe_message}") from exc

    analysis_id = derive_analysis_id(request_id, normalized_output)
    analysis_json = _build_analysis_payload(
        analysis_id=analysis_id,
        request_id=request_id,
        packet_payload=packet_payload,
        model_identity=model_identity,
        normalized_model_output=normalized_output,
    )
    analysis_md = _render_analysis_markdown(analysis_json)

    analysis_dir, reused_existing = _publish_analysis(
        root=root,
        repository_id=repository_id,
        comparison_id=comparison_id,
        analysis_id=analysis_id,
        analysis_input=packet_payload,
        analysis_json=analysis_json,
        analysis_md=analysis_md,
    )

    return {
        "analysis_id": analysis_id,
        "analysis_dir": analysis_dir,
        "request_id": request_id,
        "packet_id": packet_payload["packet_id"],
        "repository_root": str(repo_root),
        "repository_id": repository_id,
        "comparison_id": comparison_id,
        "provider": model_identity.provider,
        "model_name": model_identity.model_name,
        "model_digest": model_identity.model_digest,
        "reused_existing": reused_existing,
    }
