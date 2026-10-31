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
    DEFAULT_MODEL,
    DEFAULT_QUALITY,
    MODEL_CACHE_TTL_SECONDS,
    MODEL_DEFAULT_IMAGE_SIZES,
    REFERENCE_NETWORK_WARNING_BYTES,
)
from .errors import AsxError
from .reference_media import prepare_inputs, request_body_size, validate_request_body

_CANONICAL_MODEL = re.compile(r"^(?=.*\d)[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)+$")

class ModelCatalogClient(Protocol):
    base_url: str

    def models(self) -> tuple[list[dict[str, Any]], str | None]: ...


def normalize_model_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def resolve_model(
    models: list[dict[str, Any]], selector: str | None, task_type: str
) -> dict[str, Any]:
    eligible = [model for model in models if task_type in model.get("task_types", [])]
    available = [str(model.get("name")) for model in eligible if isinstance(model.get("name"), str)]
    if not eligible:
        raise AsxError("当前没有可用于此操作的图片模型，请查看可用模型或稍后重试。", code="no_compatible_model")
    requested = selector or DEFAULT_MODEL
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
            f"模型名称 {requested!r} 对应多个模型，请使用完整模型名称。",
            code="ambiguous_model",
            details={"matches": [str(model["name"]) for model in matches]},
        )
    message = f"模型 {requested!r} 当前不可用于此操作，请选择其他可用模型。"
    if selector is None:
        message = f"默认模型 {DEFAULT_MODEL!r} 当前不可用，请指定其他可用模型。"
    raise AsxError(message, code="model_unavailable", details={"available": available})


def _cached_models(base_url: str) -> list[dict[str, Any]] | None:
    path = cache_path()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("base_url") != base_url:
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


def cache_models(base_url: str, models: list[dict[str, Any]]) -> None:
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
                {"base_url": base_url, "fetched_at": time.time(), "items": models},
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


def capabilities(model: dict[str, Any]) -> dict[str, Any]:
    value = model.get("capabilities")
    if not isinstance(value, dict):
        raise AsxError("暂时无法读取所选模型的参数说明，请刷新模型列表或稍后重试。", code="invalid_model_catalog")
    return value


def catalog_choice(value: str, allowed: Any, field: str) -> str:
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise AsxError(
            "暂时无法读取所选模型的参数说明，请刷新模型列表或稍后重试。",
            code="invalid_model_catalog",
        )
    for item in allowed:
        if item.casefold() == value.casefold():
            return cast(str, item)
    label = {
        "image_size": "图片尺寸", "aspect_ratio": "画幅比例", "output_format": "输出格式"
    }.get(field, field)
    options = "、".join(allowed) or "暂无可用选项"
    raise AsxError(
        f"所选模型不支持{label} {value!r}，支持的选项为：{options}。",
        code="unsupported_model_capability",
        details={"field": field, "requested": value, "allowed": allowed},
    )


def image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise AsxError("输入图片需为 PNG、JPEG 或 WebP 格式，请转换格式后重试。", code="unsupported_image_format")


def validate_idempotency_key(value: str) -> str:
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AsxError("请求标识仅支持英文、数字和可见英文符号，请修改后重试。", code="invalid_idempotency_key") from exc
    if not 1 <= len(raw) <= 128 or any(byte < 0x21 or byte > 0x7E for byte in raw):
        raise AsxError(
            "请求标识需为 1–128 个英文、数字或可见英文符号，且不能包含空格或换行。",
            code="invalid_idempotency_key",
        )
    return value


def _data_url_size(value: str) -> int:
    encoded = value.rsplit(",", 1)[1]
    padding = len(encoded) - len(encoded.rstrip("="))
    return len(encoded) * 3 // 4 - padding


def task_input_warnings(body: dict[str, Any]) -> list[dict[str, Any]]:
    task_input = body.get("input")
    if not isinstance(task_input, dict):
        return []
    sources = task_input.get("reference_images", task_input.get("images", []))
    local_sources = (
        [source for source in sources if isinstance(source, str) and source.startswith("data:image/")]
        if isinstance(sources, list)
        else []
    )
    mask = task_input.get("mask")
    if isinstance(mask, str) and mask.startswith("data:image/"):
        local_sources.append(mask)
    if not local_sources:
        return []
    body_bytes = request_body_size(body)
    if body_bytes < REFERENCE_NETWORK_WARNING_BYTES:
        return []
    image_bytes = sum(_data_url_size(source) for source in local_sources)
    return [
        {
            "code": "large_reference_payload",
            "message": (
                f"处理后的 {len(local_sources)} 张本地图片共 "
                f"{image_bytes / 1024 / 1024:.2f} MiB，实际上传数据约 "
                f"{body_bytes / 1024 / 1024:.2f} MiB，网络较慢时上传可能需要更久。"
            ),
            "image_bytes": image_bytes,
            "request_body_bytes": body_bytes,
            "local_image_count": len(local_sources),
        }
    ]


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
) -> tuple[dict[str, Any], str, str | None]:
    if not 1 <= len(prompt) <= 32_000:
        raise AsxError("图片描述需为 1–32,000 个字符，请填写或缩短描述。", code="invalid_prompt")
    quality_value = quality.strip() if quality is not None else DEFAULT_QUALITY
    if not quality_value:
        raise AsxError("图片质量不能填写为空；无需指定时请省略该参数。", code="invalid_quality")
    if not 1 <= count <= 4:
        raise AsxError("单次生成数量需为 1–4 张；更多图片请使用批量生成。", code="invalid_count")
    if task_type == "image.edit" and not references:
        raise AsxError("编辑图片需要至少一张原图，请提供要修改的图片。", code="missing_edit_image")

    requested_model = (model_selector or DEFAULT_MODEL).strip()
    if not 1 <= len(requested_model) <= 160:
        raise AsxError("模型名称需为 1–160 个字符，请检查名称。", code="invalid_model")
    request_id: str | None = None
    models = _cached_models(client.base_url)
    model: dict[str, Any] | None = None
    if models is not None:
        exact = [
            item
            for item in models
            if task_type in item.get("task_types", [])
            and str(item.get("name", "")).casefold() == requested_model.casefold()
        ]
        if len(exact) == 1:
            model = exact[0]
    if model is None and model_selector is not None and not _looks_canonical(model_selector):
        if models is None:
            models, request_id = client.models()
            cache_models(client.base_url, models)
        model = resolve_model(models, model_selector, task_type)

    canonical_size = (
        image_size.strip().upper()
        if image_size
        else MODEL_DEFAULT_IMAGE_SIZES.get(
            str(model["name"] if model is not None else requested_model).casefold(), "1K"
        )
    )
    canonical_ratio = aspect_ratio.strip() if aspect_ratio else "1:1"
    canonical_format = output_format.strip().lower() if output_format else "png"
    if model is not None:
        model_capabilities = capabilities(model)
        image_sizes = model_capabilities.get("image_sizes")
        aspect_ratios = model_capabilities.get("aspect_ratios")
        output_formats = model_capabilities.get("output_formats")
        canonical_size = catalog_choice(canonical_size, image_sizes, "image_size")
        if aspect_ratio or task_type != "image.edit":
            canonical_ratio = catalog_choice(canonical_ratio, aspect_ratios, "aspect_ratio")
        canonical_format = catalog_choice(canonical_format, output_formats, "output_format")
        max_images = model_capabilities.get("max_images")
        if not isinstance(max_images, int) or count > max_images:
            raise AsxError(
                (
                    f"所选模型单次最多生成 {max_images} 张图片，请减少数量或使用批量生成。"
                    if isinstance(max_images, int)
                    else "暂时无法确认所选模型的生成数量限制，请刷新模型列表或稍后重试。"
                ),
                code="unsupported_model_capability",
            )
        max_references = model_capabilities.get("max_reference_images")
        if not isinstance(max_references, int) or len(references) > max_references:
            raise AsxError(
                (
                    f"所选模型最多接受 {max_references} 张输入图片，请减少参考图。"
                    if isinstance(max_references, int)
                    else "暂时无法确认所选模型的参考图数量限制，请刷新模型列表或稍后重试。"
                ),
                code="unsupported_model_capability",
            )
        if (
            task_type == "image.generate"
            and references
            and not model_capabilities.get("supports_reference_images")
        ):
            raise AsxError(
                "所选模型不支持参考图，请改用支持参考图的模型，或移除参考图。",
                code="unsupported_model_capability",
            )
        if mask and not model_capabilities.get("supports_mask"):
            raise AsxError(
                "所选模型不支持编辑蒙版（Mask），请改用支持蒙版的模型，或移除蒙版。", code="unsupported_model_capability"
            )

    source_sizes: list[tuple[int, int] | None] = []
    prepared, prepared_mask = prepare_inputs(
        references,
        mask=mask,
        keep_original=keep_reference_original,
        source_sizes=source_sizes,
    )
    if task_type == "image.edit" and not aspect_ratio:
        if any(size is None for size in source_sizes):
            raise AsxError(
                "无法读取 URL 输入图的比例；请指定 --aspect-ratio，或改用本地图片以自动保持画幅。",
                code="edit_aspect_ratio_required",
            )
        ratios = {
            f"{width // math.gcd(width, height)}:{height // math.gcd(width, height)}"
            for size in source_sizes if size is not None
            for width, height in [size]
        }
        if len(ratios) != 1:
            raise AsxError(
                "输入图片的比例不同；请指定输出 --aspect-ratio，或只传入要修改的图片。",
                code="ambiguous_edit_aspect_ratio",
                details={"input_aspect_ratios": sorted(ratios)},
            )
        canonical_ratio = ratios.pop()
        if model is not None:
            canonical_ratio = catalog_choice(
                canonical_ratio, capabilities(model).get("aspect_ratios"), "aspect_ratio"
            )
    task_input: dict[str, Any] = {
        "prompt": prompt,
        "count": count,
        "image_size": canonical_size,
        "aspect_ratio": canonical_ratio,
        "quality": quality_value,
        "output_format": canonical_format,
        "extra": {},
    }
    if task_type == "image.generate":
        task_input["reference_images"] = prepared
    else:
        task_input["images"] = prepared
        task_input["mask"] = prepared_mask
    body = {
        "task_type": task_type,
        "model": model["name"] if model is not None else requested_model,
        "input": task_input,
        "asset_delivery": "asynx",
    }
    validate_request_body(body)
    return body, str(body["model"]), request_id
