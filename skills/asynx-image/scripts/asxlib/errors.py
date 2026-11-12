"""Errors the CLI reports.

The service decides what an error means. Every answer says what to do next
(``next_step``), whether a submission created a task (``task_created``) and how
to explain it to the person (``user_message``). This module carries those
answers and keeps no table of error codes: a code it has never seen is handled
exactly like one it has. Its own wording covers only answers that did not come
from the service, such as a proxy's error page.
"""

from __future__ import annotations

from typing import Any

# The service's vocabulary (see the client contract). An unknown value means retry_later.
NEXT_STEPS = ("change_request", "fix_account", "retry_later", "contact_support")
# A batch stops submitting when its remaining items would meet the same answer.
PAUSING_STEPS = frozenset({"fix_account", "contact_support"})
_MAX_TEXT = 1000


def display_text(value: Any) -> str | None:
    """Service wording made safe to show: printable, bounded, never an instruction."""
    if not isinstance(value, str):
        return None
    text = "".join(char for char in value if char.isprintable() or char == "\n").strip()
    return text[:_MAX_TEXT] or None


def known_next_step(value: Any) -> str | None:
    return value if isinstance(value, str) and value in NEXT_STEPS else None


def transport_message(http_status: int | None) -> str:
    """What to say when the answer carried no explanation of its own."""
    if http_status == 401:
        return "身份验证未通过，请检查 API Key 并在终端重新配置。"
    if http_status == 403:
        return "当前账户无法执行该操作，请检查账户与密钥的使用范围，必要时联系服务支持。"
    if http_status == 404:
        return "未找到请求的内容，请检查任务 ID 或服务地址。"
    if http_status == 429:
        return "请求过于频繁或服务繁忙，请稍后再试。"
    if http_status is not None and http_status >= 500:
        return "服务暂时无法处理请求，请稍后再试；若任务可能已提交，请先确认原任务。"
    if http_status is not None and http_status >= 400:
        return "服务未接受本次请求，请检查输入素材与参数。"
    return "操作未能完成，服务未提供说明。请保留任务 ID 或请求 ID，必要时联系服务支持。"


def task_error(error: Any, status: str | None = None) -> dict[str, Any]:
    """A finished task's failure as the CLI reports it.

    ``message`` is the service's explanation for the user. Its developer text is
    dropped, so upstream wording can never reach the conversation.
    """
    source = error if isinstance(error, dict) else {}
    result: dict[str, Any] = {
        "code": str(source.get("code") or f"task_{status or 'failed'}"),
        "message": display_text(source.get("user_message"))
        or "本次生成未完成，服务未提供说明。请保留任务 ID，必要时联系服务支持。",
        "next_step": known_next_step(source.get("next_step")) or "retry_later",
    }
    for key in ("phase", "details"):
        if source.get(key) is not None:
            result[key] = source[key]
    return result


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
        task_created: bool | None = None,
        next_step: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.exit_code = exit_code
        self.http_status = http_status
        self.request_id = request_id
        self.task_id = task_id
        self.details = details
        # The service's answers; None when it gave none.
        self.task_created = task_created
        self.next_step = next_step
        # An earlier attempt under the same Idempotency-Key got no answer. The
        # service speaks only for the request it answered, so the task may exist.
        self.replayed = False

    @property
    def submission_rejected(self) -> bool:
        """Whether the submission this error answers certainly created no task."""
        return self.task_created is False and not self.task_id and not self.replayed

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.http_status is not None:
            error["http_status"] = self.http_status
        if self.details is not None:
            error["details"] = self.details
        if self.next_step is not None:
            error["next_step"] = self.next_step
        if self.submission_rejected:
            # Certain: nothing was created or charged, so nothing needs recovering.
            error["task_created"] = False
        result: dict[str, Any] = {"ok": False, "error": error}
        if self.task_id:
            result["task_id"] = self.task_id
        if self.request_id:
            result["request_id"] = self.request_id
        return result
