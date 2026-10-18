from __future__ import annotations

from typing import Any


class AsxError(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: str = "client_error",
        exit_code: int = 2,
        http_status: int | None = None,
        request_id: str | None = None,
        task_id: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.http_status = http_status
        self.request_id = request_id
        self.task_id = task_id
        self.details = details

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.http_status is not None:
            error["http_status"] = self.http_status
        if self.details is not None:
            error["details"] = self.details
        result: dict[str, Any] = {"ok": False, "error": error}
        if self.task_id:
            result["task_id"] = self.task_id
        if self.request_id:
            result["request_id"] = self.request_id
        return result
