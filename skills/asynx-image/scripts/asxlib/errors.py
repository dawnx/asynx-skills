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
    "not_found": "未找到请求的内容，请检查任务 ID，并确认使用的是创建任务时的账户。",
    # Account and key state: the request is fine, the account is not.
    "api_key_spending_limit_exceeded": "API Key 的消费额度不足，本次未创建任务。请在 LLMAPI 控制台调整该密钥的额度，或更换密钥后再提交。",
    "user_inactive": "账户当前不可用，本次未创建任务。请检查账户状态或联系服务支持。",
    "source_user_inactive": "账户当前不可用，本次未创建任务。请检查账户状态或联系服务支持。",
    "sub2api_user_required": "当前账户尚未关联 LLMAPI 账户，无法提交任务。请使用 LLMAPI 控制台创建的 API Key。",
    "insufficient_scope": "当前 API Key 没有执行该操作的权限，请在控制台检查密钥权限或更换密钥。",
    "asynx_group_changed": "该 API Key 的分组已变更，本次未创建任务。请稍后重新提交；若持续出现，请重新配置密钥。",
    "billing_key_required": "当前密钥无法用于计费，本次未创建任务。请使用 LLMAPI 控制台创建的 API Key。",
    "asynx_insufficient_balance": "账户余额不足，请充值或更换账户后再提交。",
    "asynx_quota_exhausted": "API Key 的消费额度不足，请在 LLMAPI 控制台调整额度或更换密钥。",
    "asynx_rate_5h_exceeded": "API Key 的五小时预算已用完，请稍后再提交，或调整该密钥的预算。",
    "asynx_rate_1d_exceeded": "API Key 的每日预算已用完，请稍后再提交，或调整该密钥的预算。",
    "asynx_rate_7d_exceeded": "API Key 的每周预算已用完，请稍后再提交，或调整该密钥的预算。",
    "asynx_concurrency_exceeded": "同时进行的任务已达账户上限，请等待进行中的任务完成后再提交。",
    "sub2api_rejected": "账户服务未接受本次请求，请检查账户与密钥状态，必要时联系服务支持。",
    "sub2api_unavailable": "账户服务暂时不可用，请稍后再试；已提交的任务不受影响。",
    "sub2api_invalid_response": "账户服务暂时不可用，请稍后再试；已提交的任务不受影响。",
    "sub2api_not_configured": "服务尚未完成账户配置，暂时无法使用；请联系服务支持。",
    "legacy_key_group_required": "该 API Key 尚未关联可用分组，暂时无法使用；请在 LLMAPI 控制台重新创建密钥。",
    "sub2api_account_required": "当前账户尚未关联 LLMAPI 账户，无法提交任务。请使用 LLMAPI 控制台创建的 API Key。",
    "account_unavailable": "账户当前不可用，请检查账户状态或联系服务支持。",
    "asynx_group_unavailable": "该 API Key 所在分组当前不可用，请在 LLMAPI 控制台检查分组，或更换密钥。",
    "llmapi_group_not_allowed": "该 API Key 所在分组未开通此服务，请在 LLMAPI 控制台更换分组或密钥。",
    "credential_owner_conflict": "该 API Key 的归属发生变化，暂时无法使用；请重新创建密钥后在终端重新配置。",
    "identity_email_conflict": "账户信息存在冲突，暂时无法使用；请联系服务支持。",
    "storage_integrity_failed": "参考素材保存失败，本次未创建任务。请稍后再提交。",
    "pricing_usage_required": "当前参数无法计价，本次未创建任务。请检查尺寸、数量或时长参数。",
    "internal_error": "服务暂时无法处理请求，尚不确定任务是否已创建。请先确认原任务，不要直接重新生成。",
    # Admission limits and safety checks.
    "user_concurrency_limit_exceeded": "同时进行的任务已达账户上限，本次未创建任务。请等待进行中的任务完成后再提交。",
    "api_key_concurrency_limit_exceeded": "同时进行的任务已达该 API Key 的上限，本次未创建任务。请等待进行中的任务完成后再提交。",
    "risk_blocked": "内容未通过安全审核，本次未创建任务。请调整提示词或参考素材后再提交。",
    "risk_input_too_large": "提示词或素材超过内容安全检查的长度限制，请精简后再提交。",
    "risk_unavailable": "内容安全检查暂时不可用，本次未创建任务。请稍后再提交。",
    "login_unavailable": "账户服务暂时不可用，本次未创建任务。请稍后再提交。",
    "model_not_priced": "所选模型尚未开放计费，暂不可用；请选择其他模型或联系服务支持。",
    "output_format_unavailable": "当前参数组合下暂时无法提供所选输出格式，请更换输出格式或模型。",
    "transparent_background_unavailable": "当前暂时无法提供透明背景输出，请更换模型，或改用不透明背景。",
    "unsupported_model_capability": "所选模型不支持该类型的操作，请查看模型说明后更换模型。",
    "unsupported_extra_parameter": "请求包含服务不支持的附加参数，请移除后再提交。",
    "unsupported_video_resolution": "所选视频模型不支持该分辨率，请调整后再提交。",
    "invalid_idempotency_key": "请求标识格式无效，请使用 1–128 个英文、数字或可见英文符号。",
    "validation_error": "请求参数不符合接口要求，请检查参数取值后再提交。",
    "image_count_limit_exceeded": "单个任务的输出数量超过服务上限，请减少数量，或改用批次生成。",
    "input_count_limit_exceeded": "输入素材数量超过服务上限，请减少素材后再提交。",
    # Input media the service could not accept.
    "base64_image_too_large": "单张参考图超过服务端大小上限，请缩小图片后再提交。",
    "base64_payload_too_large": "参考图合计超过服务端大小上限，请减少图片数量或缩小图片。",
    "image_pixel_limit_exceeded": "参考图像素超过服务端上限，请降低分辨率后再提交。",
    "request_body_too_large": "本次上传数据超过服务端上限，请减少参考素材或缩小图片。",
    "invalid_base64_image": "参考图内容无法解析，请确认文件可正常打开，或重新导出图片。",
    "unsupported_image_format": "参考图需为 PNG、JPEG 或 WebP 格式，请转换格式后重试。",
    "animated_image_not_supported": "不支持动画或多帧图片作为输入，请选取一帧保存为静态图片。",
    # Existing tasks and their results.
    "task_not_cancelable": "任务已结束，无法取消；是否产生费用以该任务的计费结果为准。",
    "task_content_blocked": "该任务的结果已被停用，无法下载。如有疑问请联系服务支持。",
    "asset_not_found": "未找到结果文件，请检查任务 ID，并确认使用的是创建任务时的账户。",
    "asset_expired": "结果文件已超过保留期，无法再下载。需要时请重新生成，重新生成将产生费用。",
    "asset_unavailable": "结果文件暂时无法读取，请稍后继续下载原任务，无需重新生成。",
}

# The service's answer to "did POST /v1/tasks create a task?", published in its
# client contract (task_submission). A 5xx is a confirmed rejection only for these
# codes; any other 5xx may have committed the task and must be replayed with the
# same Idempotency-Key. The contract test fails when these drift from the service.
REJECTED_5XX_CODES = frozenset({
    "callback_secret_unavailable",
    "group_model_unavailable",
    "legacy_key_group_required",
    "login_unavailable",
    "model_unavailable",
    "risk_unavailable",
    "storage_integrity_failed",
    "sub2api_invalid_response",
    "sub2api_not_configured",
    "sub2api_rejected",
    "sub2api_unavailable",
})
# 403 answers about the request itself; every other 401/402/403 is about the account.
REQUEST_403_CODES = frozenset({"group_selection_forbidden", "risk_blocked", "risk_bypass_forbidden"})
UNCONFIRMED_4XX_STATUSES = frozenset({408, 425})
# Conflicts the same request cannot resolve by changing: the account or key must.
ACCOUNT_CONFLICT_CODES = frozenset({
    "asynx_group_changed", "billing_key_required", "credential_owner_conflict", "identity_email_conflict",
})


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
                # Out-of-range exact pixels: the service lists the tiers to choose from.
                "invalid_dimensions": {"1K", "1.5K", "2K", "3K", "4K"},
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
    if code == "sub2api_rejected" and http_status in {401, 403}:
        return "API Key 无效、已停用或无权使用该服务，请在 LLMAPI 控制台检查密钥后，在终端重新配置。"
    if code in _SERVICE_MESSAGES:
        text = _SERVICE_MESSAGES[code]
        values = details if isinstance(details, dict) else {}
        if code in {"user_concurrency_limit_exceeded", "api_key_concurrency_limit_exceeded"}:
            active, limit = values.get("active_tasks"), values.get("concurrency_limit")
            if type(active) is int and type(limit) is int and 0 <= active <= 100_000 and 0 < limit <= 100_000:
                text += f"当前进行中 {active} 个，上限 {limit} 个。"
        elif code in {"base64_image_too_large", "base64_payload_too_large"}:
            maximum = values.get("max_bytes")
            if type(maximum) is int and 0 < maximum <= 1 << 40:
                text += f"服务端上限为 {maximum / (1024 * 1024):g} MiB。"
        elif code == "validation_error":
            # Schema field names only; never the submitted values or free text.
            fields: list[str] = []
            issues = values.get("issues")
            for issue in issues if isinstance(issues, list) else []:
                location = issue.get("loc") if isinstance(issue, dict) else None
                parts = [str(part) for part in location or [] if isinstance(part, (str, int))]
                if "input" in parts:
                    parts = parts[parts.index("input"):]
                name = ".".join(parts)
                if re.fullmatch(r"[A-Za-z0-9_.]{1,80}", name) and name not in fields:
                    fields.append(name)
            if fields:
                text += "涉及字段：" + "、".join(fields[:5]) + "。"
        return text
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
        # Set by the submission path; other requests do not create tasks.
        self.for_submission = False
        # An earlier attempt under the same Idempotency-Key got no definite answer.
        self.replayed = False

    @property
    def submission_outcome(self) -> str | None:
        """What an error answer to POST /v1/tasks means for the request that caused it.

        ``request``: no task exists and the same request will be refused again.
        ``account``: no task exists; the account or key must change first.
        ``later``: no task exists; the unchanged request may be accepted later.
        ``None``: unconfirmed. A task may exist, so only a replay with the same
        Idempotency-Key is safe. A task ID in the answer always means accepted.

        The service checks credentials and routing before it looks up the key, so
        a refusal of a replay says nothing about the attempt before it: once any
        attempt under a key went unanswered, later refusals stay unconfirmed.
        """
        status = self.http_status
        if self.task_id or status is None or self.replayed:
            return None
        if status == 429:
            return "later"
        if 400 <= status < 500:
            if status in UNCONFIRMED_4XX_STATUSES:
                return None
            if status in {401, 402, 403} and self.code not in REQUEST_403_CODES:
                return "account"
            return "account" if self.code in ACCOUNT_CONFLICT_CODES else "request"
        return "later" if self.code in REJECTED_5XX_CODES else None

    @property
    def submission_rejected(self) -> bool:
        return self.submission_outcome is not None

    @property
    def retry_in_place(self) -> bool:
        """Whether the HTTP layer may repeat the request before reporting the error.

        Service decisions are final for this attempt; only an unexplained transient
        answer, or a rate limit not issued by the task service, is worth repeating.
        """
        if self.submission_outcome is None:
            return True
        return self.http_status == 429 and (self.code.startswith("http_") or self.code == "rate_limited")

    def payload(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.http_status is not None:
            error["http_status"] = self.http_status
        if self.details is not None:
            error["details"] = self.details
        outcome = self.submission_outcome
        if self.for_submission and outcome is not None:
            # Tell the caller, in data, that nothing was created or charged.
            error["task_created"] = False
            error["rejection"] = outcome
        result: dict[str, Any] = {"ok": False, "error": error}
        if self.task_id:
            result["task_id"] = self.task_id
        if self.request_id:
            result["request_id"] = self.request_id
        return result
