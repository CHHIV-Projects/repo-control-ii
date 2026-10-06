from __future__ import annotations

from typing import Any

from .contracts import MAX_REVIEW_QUESTIONS, MAX_REVIEW_SIGNALS, PRIORITY_ORDER

CATEGORY_ENUM = [
    "file_change",
    "requirements_change",
    "symbol_change",
    "dependency_change",
    "call_change",
    "test_reference_change",
    "parse_or_resolution",
    "coverage_scope",
    "cross_category",
]
PRIORITY_ENUM = ["high", "medium", "low"]


class ResponseValidationError(RuntimeError):
    pass


def build_provider_response_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "summary",
            "summary_evidence_ids",
            "review_signals",
            "questions_for_human_review",
        ],
        "properties": {
            "summary": {"type": "string", "maxLength": 1000},
            "summary_evidence_ids": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 10,
            },
            "review_signals": {
                "type": "array",
                "maxItems": MAX_REVIEW_SIGNALS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["category", "review_priority", "observation", "interpretation", "evidence_ids"],
                    "properties": {
                        "category": {"type": "string", "enum": CATEGORY_ENUM},
                        "review_priority": {"type": "string", "enum": PRIORITY_ENUM},
                        "observation": {"type": "string", "maxLength": 600},
                        "interpretation": {"type": "string", "maxLength": 800},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 10,
                        },
                    },
                },
            },
            "questions_for_human_review": {
                "type": "array",
                "maxItems": MAX_REVIEW_QUESTIONS,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["review_priority", "question", "evidence_ids"],
                    "properties": {
                        "review_priority": {"type": "string", "enum": PRIORITY_ENUM},
                        "question": {"type": "string", "maxLength": 500},
                        "evidence_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 10,
                        },
                    },
                },
            },
        },
    }


def _ensure_newline_free(value: str, field_name: str) -> None:
    if "\n" in value or "\r" in value:
        raise ResponseValidationError(f"{field_name} must not contain CR/LF")


def _normalize_ids(ids: list[str], order_index: dict[str, int], field_name: str, valid_ids: set[str]) -> list[str]:
    if not isinstance(ids, list):
        raise ResponseValidationError(f"{field_name} must be an array")
    if not ids:
        raise ResponseValidationError(f"{field_name} must not be empty")
    out: list[str] = []
    seen: set[str] = set()
    for item in ids:
        if not isinstance(item, str):
            raise ResponseValidationError(f"{field_name} must contain string IDs")
        if item not in valid_ids:
            raise ResponseValidationError(f"unknown evidence id: {item}")
        if item in seen:
            raise ResponseValidationError(f"duplicate evidence id in {field_name}: {item}")
        seen.add(item)
        out.append(item)
    return sorted(out, key=lambda item: order_index[item])


def validate_and_normalize_response(
    response_payload: dict[str, Any],
    valid_evidence_ids: list[str],
    structural_delta_present: bool,
) -> dict[str, Any]:
    required_keys = {
        "summary",
        "summary_evidence_ids",
        "review_signals",
        "questions_for_human_review",
    }
    if set(response_payload.keys()) != required_keys:
        raise ResponseValidationError("response must contain exactly the required top-level fields")

    summary = response_payload["summary"]
    if not isinstance(summary, str):
        raise ResponseValidationError("summary must be a string")
    summary = summary.strip()
    if not summary:
        raise ResponseValidationError("summary must be non-empty")
    if len(summary) > 1000:
        raise ResponseValidationError("summary exceeds 1000 characters")
    _ensure_newline_free(summary, "summary")

    valid_id_set = set(valid_evidence_ids)
    order_index = {eid: idx for idx, eid in enumerate(valid_evidence_ids)}

    summary_ids = _normalize_ids(response_payload["summary_evidence_ids"], order_index, "summary_evidence_ids", valid_id_set)
    if len(summary_ids) > 10:
        raise ResponseValidationError("summary_evidence_ids exceeds maximum length")

    review_signals = response_payload["review_signals"]
    if not isinstance(review_signals, list):
        raise ResponseValidationError("review_signals must be an array")
    if len(review_signals) > MAX_REVIEW_SIGNALS:
        raise ResponseValidationError("review_signals exceeds maximum length")

    normalized_signals: list[dict[str, Any]] = []
    for idx, signal in enumerate(review_signals):
        if not isinstance(signal, dict):
            raise ResponseValidationError("review_signals entries must be objects")
        if set(signal.keys()) != {"category", "review_priority", "observation", "interpretation", "evidence_ids"}:
            raise ResponseValidationError("review signal fields are invalid")

        category = signal["category"]
        priority = signal["review_priority"]
        observation = signal["observation"]
        interpretation = signal["interpretation"]
        if category not in CATEGORY_ENUM:
            raise ResponseValidationError(f"invalid review signal category at index {idx}")
        if priority not in PRIORITY_ENUM:
            raise ResponseValidationError(f"invalid review signal priority at index {idx}")
        if not isinstance(observation, str) or not isinstance(interpretation, str):
            raise ResponseValidationError("signal observation/interpretation must be strings")

        observation = observation.strip()
        interpretation = interpretation.strip()
        if not observation:
            raise ResponseValidationError("signal observation must be non-empty")
        if not interpretation:
            raise ResponseValidationError("signal interpretation must be non-empty")
        if len(observation) > 600:
            raise ResponseValidationError("signal observation exceeds 600 characters")
        if len(interpretation) > 800:
            raise ResponseValidationError("signal interpretation exceeds 800 characters")
        _ensure_newline_free(observation, "review_signals[].observation")
        _ensure_newline_free(interpretation, "review_signals[].interpretation")

        signal_ids = _normalize_ids(signal["evidence_ids"], order_index, "review_signals[].evidence_ids", valid_id_set)
        if len(signal_ids) > 10:
            raise ResponseValidationError("review_signals[].evidence_ids exceeds maximum length")
        normalized_signals.append(
            {
                "category": category,
                "review_priority": priority,
                "observation": observation,
                "interpretation": interpretation,
                "evidence_ids": signal_ids,
            }
        )

    questions = response_payload["questions_for_human_review"]
    if not isinstance(questions, list):
        raise ResponseValidationError("questions_for_human_review must be an array")
    if len(questions) > MAX_REVIEW_QUESTIONS:
        raise ResponseValidationError("questions_for_human_review exceeds maximum length")

    normalized_questions: list[dict[str, Any]] = []
    for idx, question in enumerate(questions):
        if not isinstance(question, dict):
            raise ResponseValidationError("questions_for_human_review entries must be objects")
        if set(question.keys()) != {"review_priority", "question", "evidence_ids"}:
            raise ResponseValidationError("question fields are invalid")

        priority = question["review_priority"]
        question_text = question["question"]
        if priority not in PRIORITY_ENUM:
            raise ResponseValidationError(f"invalid question priority at index {idx}")
        if not isinstance(question_text, str):
            raise ResponseValidationError("question text must be a string")

        question_text = question_text.strip()
        if not question_text:
            raise ResponseValidationError("question text must be non-empty")
        if len(question_text) > 500:
            raise ResponseValidationError("question text exceeds 500 characters")
        _ensure_newline_free(question_text, "questions_for_human_review[].question")

        question_ids = _normalize_ids(question["evidence_ids"], order_index, "questions_for_human_review[].evidence_ids", valid_id_set)
        if len(question_ids) > 10:
            raise ResponseValidationError("questions_for_human_review[].evidence_ids exceeds maximum length")
        normalized_questions.append(
            {
                "review_priority": priority,
                "question": question_text,
                "evidence_ids": question_ids,
            }
        )

    if not structural_delta_present:
        if normalized_signals:
            raise ResponseValidationError("zero-delta comparison response must not include review_signals")
        if normalized_questions:
            raise ResponseValidationError("zero-delta comparison response must not include questions_for_human_review")
    else:
        if not normalized_signals:
            raise ResponseValidationError("non-zero comparison response must include at least one review signal")

    normalized_signals.sort(
        key=lambda item: (
            PRIORITY_ORDER[item["review_priority"]],
            item["category"],
            tuple(item["evidence_ids"]),
            item["observation"],
        )
    )
    normalized_questions.sort(
        key=lambda item: (
            PRIORITY_ORDER[item["review_priority"]],
            tuple(item["evidence_ids"]),
            item["question"],
        )
    )

    return {
        "summary": summary,
        "summary_evidence_ids": summary_ids,
        "review_signals": normalized_signals,
        "questions_for_human_review": normalized_questions,
    }
