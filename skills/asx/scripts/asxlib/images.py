from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import time
from typing import Any, Protocol, cast

from .config import cache_path
from .constants import (
    DEFAULT_MODEL,
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
        raise AsxError(f"No image model supports {task_type}", code="no_compatible_model")
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
            f"Model selector {requested!r} is ambiguous",
            code="ambiguous_model",
            details={"matches": [str(model["name"]) for model in matches]},
        )
    message = f"Model {requested!r} is unavailable"
    if selector is None:
        message = f"Default model {DEFAULT_MODEL!r} is unavailable; choose a model explicitly"
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
        raise AsxError("Selected model has invalid capabilities", code="invalid_model_catalog")
    return value


def catalog_choice(value: str, allowed: Any, field: str) -> str:
    if not isinstance(allowed, list) or not all(isinstance(item, str) for item in allowed):
        raise AsxError(
            f"Selected model has invalid {field} capabilities",
            code="invalid_model_catalog",
        )
    for item in allowed:
        if item.casefold() == value.casefold():
            return cast(str, item)
    raise AsxError(
        f"Selected model does not support {field}={value!r}",
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
    raise AsxError("Local inputs must be PNG, JPEG, or WebP images", code="unsupported_image_format")


def validate_idempotency_key(value: str) -> str:
    try:
        raw = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AsxError("Idempotency key must be printable ASCII", code="invalid_idempotency_key") from exc
    if not 1 <= len(raw) <= 128 or any(byte < 0x21 or byte > 0x7E for byte in raw):
        raise AsxError(
            "Idempotency key must contain 1-128 printable ASCII bytes without whitespace",
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
                f"{image_bytes / 1024 / 1024:.2f} MiB，Base64 请求体为 "
                f"{body_bytes / 1024 / 1024:.2f} MiB，弱网上传可能明显变慢"
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
        raise AsxError("Prompt must contain 1-32,000 characters", code="invalid_prompt")
    quality_value = quality.strip() if quality else "standard"
    if not quality_value:
        raise AsxError("Quality cannot be empty", code="invalid_quality")
    if not 1 <= count <= 4:
        raise AsxError("Count must be between 1 and 4", code="invalid_count")
    if task_type == "image.edit" and not references:
        raise AsxError("Image editing requires at least one input image", code="missing_edit_image")

    requested_model = (model_selector or DEFAULT_MODEL).strip()
    if not 1 <= len(requested_model) <= 160:
        raise AsxError("Model name must contain 1-160 characters", code="invalid_model")
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
        else MODEL_DEFAULT_IMAGE_SIZES.get(requested_model.casefold(), "1K")
    )
    canonical_ratio = aspect_ratio.strip() if aspect_ratio else "1:1"
    canonical_format = output_format.strip().lower() if output_format else "png"
    if model is not None:
        model_capabilities = capabilities(model)
        image_sizes = model_capabilities.get("image_sizes")
        aspect_ratios = model_capabilities.get("aspect_ratios")
        output_formats = model_capabilities.get("output_formats")
        if image_size is None:
            if not isinstance(image_sizes, list) or not image_sizes:
                raise AsxError("Selected model has invalid image_size capabilities", code="invalid_model_catalog")
            canonical_size = str(image_sizes[0])
        else:
            canonical_size = catalog_choice(image_size, image_sizes, "image_size")
        if aspect_ratio is None:
            if not isinstance(aspect_ratios, list) or not aspect_ratios:
                raise AsxError("Selected model has invalid aspect_ratio capabilities", code="invalid_model_catalog")
            canonical_ratio = str(aspect_ratios[0])
        else:
            canonical_ratio = catalog_choice(aspect_ratio, aspect_ratios, "aspect_ratio")
        if output_format is None:
            if not isinstance(output_formats, list) or not output_formats:
                raise AsxError("Selected model has invalid output_format capabilities", code="invalid_model_catalog")
            canonical_format = str(output_formats[0])
        else:
            canonical_format = catalog_choice(output_format, output_formats, "output_format")
        max_images = model_capabilities.get("max_images")
        if not isinstance(max_images, int) or count > max_images:
            raise AsxError(
                f"Selected model supports at most {max_images} output images",
                code="unsupported_model_capability",
            )
        max_references = model_capabilities.get("max_reference_images")
        if not isinstance(max_references, int) or len(references) > max_references:
            raise AsxError(
                f"Selected model supports at most {max_references} input images",
                code="unsupported_model_capability",
            )
        if (
            task_type == "image.generate"
            and references
            and not model_capabilities.get("supports_reference_images")
        ):
            raise AsxError(
                "Selected model does not support reference images",
                code="unsupported_model_capability",
            )
        if mask and not model_capabilities.get("supports_mask"):
            raise AsxError(
                "Selected model does not support masks", code="unsupported_model_capability"
            )

    prepared, prepared_mask = prepare_inputs(
        references,
        mask=mask,
        keep_original=keep_reference_original,
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
