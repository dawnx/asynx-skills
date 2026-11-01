from __future__ import annotations

from typing import Any

_SERVICE_MESSAGES = {
    "invalid_api_key": "API Key 无效或已失效，请在终端重新配置。",
    "missing_api_key": "尚未配置 API Key，请先在终端运行 configure 完成配置。",
    "insufficient_balance": "账户余额不足，请充值或更换账户后再提交。",
    "billing_unavailable": "暂时无法确认账户计费状态，请稍后查询原任务，避免重复提交。",
    "invalid_user": "账户当前不可用，请检查账户状态或联系服务支持。",
    "sub2api_user_inactive": "账户当前不可用，请检查账户状态或联系服务支持。",
    "model_not_found": "未找到所选模型，请查看可用模型后重新选择。",
    "model_unavailable": "所选模型暂时不可用，请稍后重试或更换模型。",
    "input_asset_unavailable": "参考素材读取失败，请确认文件或链接可正常访问。",
    "input_asset_too_large": "参考素材超过大小限制，请压缩后再提交。",
    "invalid_input_asset": "参考素材不可用，请检查格式和内容。",
    "unsupported_image_parameters": "所选模型不支持当前图片参数，请调整尺寸或画幅比例后再提交。",
    "unsupported_asset_delivery": "暂时无法获取本次结果文件，请保留任务 ID 并联系服务支持。",
    "provider_request_rejected": "生成服务未接受本次请求，请检查所选模型、提示词和参考素材。",
    "provider_attempt_timeout": "生成服务超时仍未完成，请查询原任务确认最终结果。",
    "provider_cost_configuration_error": "服务暂时无法处理所选模型，请联系服务支持。",
    "provider_rate_limited": "生成服务繁忙，请稍后查询原任务状态，避免重复提交。",
    "retry_disabled": "本次生成未完成，服务已停止重试。重新生成将产生费用。",
    "task_retry_stopped": "本次生成未完成，服务已停止重试。重新生成将产生费用。",
    "task_timeout": "任务因超时未完成。重新生成将产生费用，请先确认原任务状态。",
    "task_canceled": "任务已取消，是否产生费用以原任务的计费结果为准。",
    "canceled": "任务已取消，是否产生费用以原任务的计费结果为准。",
    "task_failed": "本次生成未完成，服务未提供可确认的原因。请保留任务 ID，必要时联系服务支持。",
    "idempotency_conflict": "本次请求与已有提交记录不一致，请先确认原任务，避免重复生成。",
    "task_not_found": "未找到该任务，请检查任务 ID，并确认使用的是创建任务时的账户。",
}


def service_error_message(code: str, *, http_status: int | None = None, message: Any = None) -> str:
    """Describe known service failures without forwarding arbitrary upstream text."""
    if code == "provider_request_rejected" and isinstance(message, str):
        # Keep an explicitly identified parameter actionable without echoing values.
        for parameter, label in (("quality", "质量"), ("aspect_ratio", "画幅比例"), ("image_size", "图片尺寸")):
            if f"Invalid value for {parameter}:" in message or f"param: {parameter}" in message:
                return f"生成服务不接受当前的{label}（{parameter}）参数，请按所选模型支持的值调整后再提交。"
    if code in _SERVICE_MESSAGES:
        return _SERVICE_MESSAGES[code]
    if http_status == 401:
        return "身份验证未通过，请检查 API Key 并在终端重新配置。"
    if http_status == 403:
        return "当前账户无法执行该操作，请检查账户与模型的使用范围，必要时联系服务支持。"
    if http_status == 404:
        return "未找到请求的内容，请检查任务 ID 或服务地址。"
    if http_status == 429:
        return "请求过于频繁或服务繁忙，请稍后查询原任务，避免重复提交。"
    if http_status is not None and http_status >= 500:
        return "服务暂时无法处理请求，请稍后查询原任务；若任务尚未创建成功，请稍后再试。"
    if http_status in {400, 413, 422}:
        return "服务未接受本次请求，请检查输入素材与参数是否符合所选模型的要求。"
    return "操作未能完成，服务未提供可确认的原因。请保留任务 ID 或请求 ID，必要时联系服务支持。"


def friendly_task_error(error: Any, status: str | None = None) -> dict[str, Any]:
    result = dict(error) if isinstance(error, dict) else {}
    code = str(result.get("code") or f"task_{status or 'failed'}")
    result["code"] = code
    result["message"] = service_error_message(code, message=result.get("message"))
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
