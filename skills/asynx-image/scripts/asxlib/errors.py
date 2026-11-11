from __future__ import annotations

import re
from typing import Any

_SERVICE_MESSAGES = {
    "invalid_api_key": "API Key 无效或已失效，请在终端重新配置。",
    "missing_api_key": "尚未配置 API Key，请先在终端运行 configure 完成配置。",
    "insufficient_balance": "账户余额不足，请充值或更换账户后再提交。",
    "billing_unavailable": "暂时无法确认账户计费状态，请稍后查询原任务，避免重复提交。",
    "invalid_user": "账户当前不可用，请检查账户状态或联系服务支持。",
    "sub2api_user_inactive": "账户当前不可用，请检查账户状态或联系服务支持。",
    "model_not_found": "未找到所选模型，请查看可用模型后重新选择。",
    "group_model_unavailable": "当前模型暂不可用，请选择其他模型，或联系服务提供方。",
    "model_unavailable": "所选模型暂时不可用，请稍后重试或更换模型。",
    "input_asset_unavailable": "参考素材读取失败，请确认文件或链接可正常访问。",
    "input_asset_too_large": "参考素材超过大小限制，请压缩后再提交。",
    "invalid_input_asset": "参考素材不可用，请检查格式和内容。",
    "unsupported_image_parameters": "所选模型不支持当前图片参数，请查阅模型说明后确认调整；不会自动修改或重新提交。",
    "invalid_image_size": "图片尺寸格式无效，请使用宽x高（例如 1536x1024）或明确指定 auto。",
    "conflicting_image_dimensions": "精确尺寸不能与尺寸档位或画幅比例同时指定，请选择一种方式。",
    "unsupported_image_size": "所选模型不支持请求的图片尺寸，请调整尺寸后再生成。",
    "provider_result_unavailable": "暂时无法获取本次结果，请保留任务 ID 并联系服务提供方，避免重复生成。",
    "invalid_provider_result": "本次结果暂时无法交付，请保留任务 ID 并联系服务提供方，避免重复生成。",
    "unsupported_asset_delivery": "暂时无法获取本次结果文件，请保留任务 ID 并联系服务支持。",
    "provider_request_rejected": "生成服务未接受本次请求，请检查所选模型、提示词和参考素材。",
    "provider_endpoint_unavailable": "生成服务的接口或模型路由不可用，请联系服务支持检查渠道配置。",
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


def _safe_size(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"auto|[1-9]\d{0,7}x[1-9]\d{0,7}", value) is not None


def service_error_message(code: str, *, http_status: int | None = None, message: Any = None, details: Any = None) -> str:
    """Describe known service failures without forwarding arbitrary upstream text."""
    if code == "unsupported_image_parameters" and isinstance(details, dict):
        reason = details.get("reason")
        maximum = details.get("maximum")
        limits = {
            "count_limit": "本次最多可生成",
            "reference_limit": "最多可提供参考图",
            "total_image_limit": "参考图与输出图合计最多",
        }
        text = {
            "unsupported_size": "所选模型不支持此尺寸档位。",
            "invalid_dimensions": "图片尺寸或画幅比例不符合所选模型的要求。",
            "unsupported_format": "所选模型不支持此输出格式。",
            "unsupported_background": "所选模型不支持此背景设置。",
            "transparent_requires_single_alpha_png": "透明背景需要单张带 Alpha 通道的参考图，并使用 PNG 输出。",
            "unsupported_option": "所选模型不支持当前附加参数。",
            "unsupported_sequential": "所选模型不支持组图参数。",
            "invalid_sequential": "组图参数组合不正确，请按模型说明设置数量与组图模式。",
        }.get(reason) if isinstance(reason, str) else None
        if isinstance(reason, str) and reason in limits and type(maximum) is int and 0 < maximum <= 1000:
            text = f"{limits[reason]} {maximum} 张。"
        if reason == "unsupported_option":
            labels = {
                "input.quality": "质量（quality）",
                "input.complexity": "规划复杂度（complexity）",
                "input.moderation": "审核设置（moderation）",
                "input.output_compression": "输出压缩（output_compression）",
                "input.frame_count": "动画帧数（frame_count）",
                "input.frame_duration_ms": "动画帧时长（frame_duration_ms）",
                "input.svg": "SVG 配置",
                "input.safety_identifier": "安全标识（safety_identifier）",
                "input.extra.stream": "流式输出（stream）",
                "input.extra.tools": "工具（tools）",
                "input.extra.layer_decomposition": "图层拆分（layer_decomposition）",
                "input.extra.optimize_prompt_options": "提示词优化（optimize_prompt_options）",
                "input.extra.sequential_image_generation": "组图模式（sequential_image_generation）",
                "input.extra.sequential_image_generation_options": "组图配置（sequential_image_generation_options）",
            }
            field = details.get("field")
            if isinstance(field, str) and field in labels:
                text = f"所选模型不支持当前的{labels[field]}参数。"
        if text:
            allowed = details.get("allowed")
            safe_values = {
                "unsupported_size": {"1K", "1.5K", "2K", "3K", "4K"},
                "unsupported_format": {"png", "jpeg", "webp", "svg", "gif"},
                "unsupported_background": {"opaque", "transparent", "auto"},
                "unsupported_option": {"standard", "fast"}
                if details.get("field") == "input.extra.optimize_prompt_options" else set(),
            }.get(reason, set())
            if isinstance(allowed, list) and 0 < len(allowed) <= 10 and all(
                isinstance(value, str) and value in safe_values for value in allowed
            ):
                text += "可选值：" + "、".join(allowed) + "。"
            return text
    if code == "unsupported_image_size":
        values = details if isinstance(details, dict) else {}
        requested = values.get("requested_size")
        result = (
            f"所选模型不支持请求的图片尺寸 {requested}。"
            if _safe_size(requested) else "所选模型不支持请求的图片尺寸。"
        )
        tier = values.get("requested_tier")
        tier_failure = values.get("size_source") == "tier" and isinstance(tier, str) and re.fullmatch(r"[1-9]\d{0,2}(?:\.\d{1,2})?[kK]", tier) is not None
        if tier_failure:
            result = f"当前模型暂时无法满足 {tier} 尺寸档位。"
            ratio = values.get("requested_aspect_ratio")
            if isinstance(ratio, str) and re.fullmatch(r"\d{1,8}(?:\.\d{1,6})?:\d{1,8}(?:\.\d{1,6})?", ratio) and all(float(part) > 0 for part in ratio.split(":")):
                result = f"当前模型暂时无法满足 {tier}、{ratio} 的尺寸要求。"
        allowed = values.get("allowed_sizes")
        if isinstance(allowed, list) and 0 < len(allowed) <= 32 and all(_safe_size(x) for x in allowed):
            result += "服务返回的可选尺寸：" + "、".join(allowed) + "。"
            if tier_failure:
                return result + "请联系服务支持核对尺寸适配，无需猜测其他像素值；不会自动换尺寸或重新提交。"
            return result + "请确认调整后的尺寸再生成，不会自动修改或重新提交。"
        if tier_failure:
            return result + "请联系服务支持核对尺寸适配，无需猜测其他像素值；不会自动换尺寸或重新提交。"
        return result + "服务未提供可选尺寸。可确认是否接受按标准档位生成后在本地调整；不会自动试尺寸或重新提交。"
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
    result["message"] = service_error_message(code, message=result.get("message"), details=result.get("details"))
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

    @property
    def submission_rejected(self) -> bool:
        # This gateway error occurs before task creation, regardless of its 503 status.
        # An accompanying task ID always takes precedence over this classification.
        return not self.task_id and (
            (self.code == "group_model_unavailable" and self.http_status in {404, 503})
            or (self.code in {"invalid_image_size", "conflicting_image_dimensions", "unsupported_image_size", "unsupported_image_parameters"}
                and self.http_status in {400, 422})
        )

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
