from __future__ import annotations


class WorkflowReasonError(RuntimeError):
    def __init__(self, code: str, safe_message: str, *, commit_id: str | None = None) -> None:
        self.code = code
        self.safe_message = safe_message
        self.commit_id = commit_id
        super().__init__(f"workflow error [{code}]: {safe_message}")
