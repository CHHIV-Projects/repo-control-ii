from __future__ import annotations

import json
from typing import Any
import urllib.error
import urllib.parse
import urllib.request

from .provider import IntelligenceError
from .service import normalize

BASE_URL = "http://127.0.0.1:8765"


def request(path: str, arguments: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = BASE_URL + path
    if arguments:
        url += "?" + urllib.parse.urlencode(arguments)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    message = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(message, timeout=15) as response:
            raw = response.read(512001)
            if len(raw) > 512000:
                raise IntelligenceError("Repo Control response exceeds the transport byte cap.")
            value = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise IntelligenceError(f"Repo Control intelligence request failed: {exc}") from exc
    if not isinstance(value, dict) or value.get("error") and "outcome" not in value:
        raise IntelligenceError(f"Repo Control intelligence error: {value}")
    return value


def preconditions() -> dict[str, Any]:
    status = request("/api/architect/v1/status")
    repository = status["repository"]
    return {"expected_repository_key": repository["key"], "expected_generation": repository["registry_generation"]}


def query(arguments: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize(arguments)
    selected = preconditions()
    return request("/api/coder/v1/intelligence", {
        **{k: str(v) for k, v in selected.items()},
        "request": json.dumps(normalized, separators=(",", ":")),
    })


def rate(use_id: str, rating: str, inspected: bool | None) -> dict[str, Any]:
    return request("/api/coder/v1/intelligence/rating", payload={
        **preconditions(), "use_id": use_id, "rating": rating, "inspected": inspected,
    })
