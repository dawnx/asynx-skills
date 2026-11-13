from __future__ import annotations

import json
import math
import os
import re
import stat
import tempfile
import time
from typing import Any, Protocol, cast

from .config import cache_path
from .constants import (
    MAX_REFERENCE_TOTAL_BYTES,
    MODEL_CACHE_TTL_SECONDS,
    SERVICE_DEFAULT_MODEL,
)
from .errors import AsxError
from .image_options import resolved_image_options
from .reference_media import prepare_inputs

_CANONICAL_MODEL = re.compile(r"^(?=.*\d)[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+$")

class ModelCatalogClient(Protocol):
    base_url: str

    def models(self) -> tuple[list[dict[str, Any]], str | None]: ...


def normalize_model_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def resolve_model(
    models: list[dict[str, Any]], selector: str, task_type: str
) -> dict[str, Any]:
    """Find the catalog entry a user named, perhaps loosely. Never picks a default."""
    eligible = [model for model in models if task_type in model.get("task_types", [])]
    available = [str(model.get("name")) for model in eligible if isinstance(model.get("name"), str)]
    if not eligible:
        raise AsxError("当前没有可用于该操作的图片模型，请查看可用模型或稍后重试。", code="no_compatible_model")
    requested = selector
    exact = [
        model
        for model in eligible
        if str(model.get("name", "")).casefold() == requested.casefold()
    ]
    if len(exact) == 1:
        return exact[0]
    normalized = normalize_model_name(requested)
    matches = [
        model
        for model in eligible
        if normalized and normalized in normalize_model_name(str(model.get("name", "")))
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AsxError(
            f"模型名 {requested!r} 匹配到多个模型，请使用完整模型名称。",
            code="ambiguous_model",
            details={"matches": [str(model["name"]) for model in matches]},
        )
    raise AsxError(
        f"模型 {requested!r} 当前不可用于该操作，请选择其他可用模型。",
        code="model_unavailable", details={"available": available},
    )


def _cached_models(base_url: str, *, scope: str | None = None) -> list[dict[str, Any]] | None:
    if not isinstance(scope, str) or re.fullmatch(r"[a-f0-9]{64}", scope) is None:
        return None
    path = cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(payload, dict) or payload.get("base_url") != base_url
        or payload.get("scope") != scope
    ):
        return None
    fetched_at = payload.get("fetched_at")
    items = payload.get("items")
    if (
        not isinstance(fetched_at, (int, float))
        or time.time() - float(fetched_at) > MODEL_CACHE_TTL_SECONDS
        or not isinstance(items, list)
        or not all(isinstance(item, dict) for item in items)
    ):
        return None
    return cast(list[dict[str, Any]], items)


def cache_models(base_url: str, models: list[dict[str, Any]], *, scope: str | None = None) -> None:
    if not isinstance(scope, str) or re.fullmatch(r"[a-f0-9]{64}", scope) is None:
        return
    path = cache_path()
    temporary: str | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="models-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(
                {"base_url": base_url, "scope": scope, "fetched_at": time.time(), "items": models},
                handle,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            handle.write("\n")
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
    except OSError:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _looks_canonical(selector: str) -> bool:
    return bool(_CANONICAL_MODEL.fullmatch(selector.strip()))


def validate_idempotency_key(value: str) -> str:
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AsxError("请求标识仅支持英文、数字和可见英文符号，请修改后重试。", code="invalid_idempotency_key") from exc
    if not 1 <= len(raw) <= 128 or any(byte < 0x21 or byte > 0x7E for byte in raw):
        raise AsxError(
            "请求标识须为 1–128 个英文、数字或可见英文符号，不可包含空格或换行。",
            code="invalid_idempotency_key",
        )
    return value


def build_task(
    client: ModelCatalogClient,
    *,
    task_type: str,
    model_selector: str | None,
    prompt: str,
    image_size: str | None,
    aspect_ratio: str | None,
    quality: str | None,
    count: int,
    output_format: str | None,
    references: list[str],
    mask: str | None = None,
    keep_reference_original: bool = False,
    options: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    if not 1 <= len(prompt) <= 32_000:
        raise AsxError("图片描述须为 1–32,000 个字符，请填写或缩短后提交。", code="invalid_prompt")
    quality_value = quality.strip() if quality is not None else None
    if quality_value is not None and not quality_value:
        raise AsxError("图片质量不能为空；无需指定时请省略该参数。", code="invalid_quality")
    if not 1 <= count <= 10:
        raise AsxError("单次生成数量须为 1–10 张；更多图片请使用批量生成。", code="invalid_count")
    if task_type == "image.edit" and not references:
        raise AsxError("编辑图片至少需要一张原图，请提供要修改的图片。", code="missing_edit_image")

    canonical_format = output_format.strip().lower() if output_format else "png"
    # No model named: the request omits it and the service applies its configured
    # default for this task type and output format.
    requested_model = model_selector.strip() if model_selector is not None else None
    if requested_model is not None and not 1 <= len(requested_model) <= 160:
        raise AsxError("模型名称须为 1–160 个字符，请检查名称是否正确。", code="invalid_model")
    request_id: str | None = None
    model: dict[str, Any] | None = None
    if requested_model is not None:
        models = _cached_models(client.base_url, scope=getattr(client, "cache_scope", None))
        if models is not None:
            exact = [
                item
                for item in models
                if task_type in item.get("task_types", [])
                and str(item.get("name", "")).casefold() == requested_model.casefold()
            ]
            if len(exact) == 1:
                model = exact[0]
        if model is None and not _looks_canonical(requested_model):
            if models is None:
                models, request_id = client.models()
                cache_models(client.base_url, models, scope=getattr(client, "cache_scope", None))
            model = resolve_model(models, requested_model, task_type)
    selected_model = str(model["name"]) if model is not None else requested_model

    canonical_size = image_size.strip() if image_size is not None else None
    canonical_ratio = aspect_ratio.strip() if aspect_ratio is not None else None
    options = dict(options or {})
    if canonical_size == "" or canonical_ratio == "":
        raise AsxError("图片尺寸与画幅比例不能填写为空；无需指定时请省略对应参数。", code="invalid_image_options")
    if options.get("size") is not None and (image_size is not None or aspect_ratio is not None):
        raise AsxError("精确尺寸不能与尺寸档位或画幅比例同时指定。", code="conflicting_image_options")
    if canonical_format not in {"png", "jpeg", "webp", "svg", "gif"}:
        raise AsxError("输出格式须为 PNG、JPEG、WebP、SVG 或 GIF。", code="unsupported_image_format")
    options = resolved_image_options(
        canonical_format, options,
        image_size=canonical_size,
        aspect_ratio=canonical_ratio,
    )
    if canonical_format == "svg":
        svg = options.get("svg", {})
        if not isinstance(svg, dict):
            raise AsxError("SVG 参数须为 JSON 对象。", code="invalid_image_options")
        # The service supplies the SVG author when none is named.
        options["svg"] = dict(svg)
        if not options["svg"]:
            del options["svg"]
    if options.get("markup") and (mask or task_type != "image.edit"):
        raise AsxError("Markup 仅用于编辑，且不能与 Mask 同时使用。", code="conflicting_image_options")
    source_sizes: list[tuple[int, int] | None] = []
    prepared_bytes: list[int] = []
    prepared, prepared_mask = prepare_inputs(
        references,
        mask=mask,
        keep_original=keep_reference_original,
        source_sizes=source_sizes,
        prepared_bytes=prepared_bytes,
    )
    if task_type == "image.edit" and not aspect_ratio and not options.get("size"):
        if any(size is None for size in source_sizes):
            raise AsxError(
                "无法读取 URL 输入图的画幅比例；请通过 --aspect-ratio 指定，或改用本地图片以自动保持画幅。",
                code="edit_aspect_ratio_required",
            )
        ratios = {
            f"{width // math.gcd(width, height)}:{height // math.gcd(width, height)}"
            for size in source_sizes if size is not None
            for width, height in [size]
        }
        if len(ratios) != 1:
            raise AsxError(
                "多张输入图的画幅比例不一致；请通过 --aspect-ratio 指定输出比例，或仅传入要修改的图片。",
                code="ambiguous_edit_aspect_ratio",
                details={"input_aspect_ratios": sorted(ratios)},
            )
        canonical_ratio = ratios.pop()
    task_input: dict[str, Any] = {
        "prompt": prompt,
        "count": count,
        "output_format": canonical_format,
        "extra": {},
    }
    if canonical_size is not None:
        task_input["image_size"] = canonical_size
    if canonical_ratio is not None:
        task_input["aspect_ratio"] = canonical_ratio
    if quality_value is not None:
        task_input["quality"] = quality_value
    if options.get("markup"):
        from .reference_media import prepare_markup
        markup = prepare_markup(options["markup"], references[0], keep_original=keep_reference_original)
        options["markup"] = markup.source
        if sum(prepared_bytes) + (markup.size_bytes or 0) > MAX_REFERENCE_TOTAL_BYTES:
            raise AsxError("本地图片与标注处理后的总大小超过 20 MiB，请减少图片数量或缩小图片。", code="reference_images_total_too_large")
    task_input.update(options)
    if options.get("size"):
        task_input.pop("image_size", None)
        task_input.pop("aspect_ratio", None)
    if task_type == "image.generate":
        task_input["reference_images"] = prepared
    else:
        task_input["images"] = prepared
        task_input["mask"] = prepared_mask
    body: dict[str, Any] = {"task_type": task_type, "input": task_input, "asset_delivery": "asynx"}
    if selected_model is not None:
        body["model"] = selected_model
    return body, selected_model or SERVICE_DEFAULT_MODEL, request_id
