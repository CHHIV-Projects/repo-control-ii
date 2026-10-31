from __future__ import annotations

from typing import Any


def is_correction_mode(workflow: dict[str, Any]) -> bool:
    if workflow.get("phase") != "IN_PROGRESS":
        return False
    for event in reversed(workflow.get("event_history", [])):
        if event.get("event_type") == "correction_requested":
            return True
        if event.get("event_type") in {"implementation_started", "implementation_resumed"}:
            return False
    return False


def get_authorization_status(phase: str, is_correction: bool = False) -> dict[str, Any]:
    if phase == "PLANNING":
        return {
            "authorized": False,
            "can_preflight": False,
            "status": "NOT_AUTHORIZED",
            "message": "NO — milestone is in PLANNING; Product Owner prompt review and approval required before implementation.",
        }
    if phase == "READY":
        return {
            "authorized": False,
            "can_preflight": True,
            "status": "PREFLIGHT_ONLY",
            "message": "NO (preflight only) — prompt is READY; implementation begins only after explicit lifecycle start.",
        }
    if phase == "IN_PROGRESS":
        if is_correction:
            return {
                "authorized": True,
                "can_preflight": True,
                "status": "AUTHORIZED_CORRECTION",
                "message": "YES (correction mode) — work is limited to the requested bounded correction.",
            }
        return {
            "authorized": True,
            "can_preflight": True,
            "status": "AUTHORIZED",
            "message": "YES — implementation and validation are authorized.",
        }
    if phase == "WAITING":
        return {
            "authorized": False,
            "can_preflight": False,
            "status": "NOT_AUTHORIZED",
            "message": "NO — implementation is paused in WAITING; resolve recorded condition and Product Owner resume required.",
        }
    if phase == "REVIEW":
        return {
            "authorized": False,
            "can_preflight": False,
            "status": "NOT_AUTHORIZED",
            "message": "NO — implementation is under REVIEW; no new changes authorized unless Product Owner requests bounded correction.",
        }
    if phase == "CLOSED":
        return {
            "authorized": False,
            "can_preflight": False,
            "status": "NOT_AUTHORIZED",
            "message": "NO — milestone is CLOSED; no further implementation authorized under this milestone.",
        }
    return {
        "authorized": False,
        "can_preflight": False,
        "status": "UNKNOWN",
        "message": f"NO — unknown phase: {phase}",
    }
