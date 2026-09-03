from __future__ import annotations

import base64
import binascii
import re
from pathlib import Path
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

from .constants import DEFAULT_MODEL, MAX_IMAGE_INPUTS
from .errors import AsxError


class ModelCatalogClient(Protocol):
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


def image_source(value: str) -> str:
    if value.startswith("data:image/"):
        try:
            header, encoded = value.split(",", 1)
            if not header.endswith(";base64"):
                raise ValueError
            image_mime(base64.b64decode(encoded, validate=True))
        except (ValueError, binascii.Error) as exc:
            raise AsxError("Image Data URL is invalid", code="invalid_base64_image") from exc
        return value
    parts = urlsplit(value)
    if parts.scheme in {"http", "https"} and parts.hostname:
        return value
    path = Path(value).expanduser()
    if not path.is_file():
        raise AsxError(f"Image file does not exist: {path}", code="image_not_found")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AsxError(f"Cannot read image file {path}: {exc}", code="image_read_failed") from exc
    mime = image_mime(data)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


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


def build_task(
    client: ModelCatalogClient,
    *,
    task_type: str,
    model_selector: str | None,
    prompt: str,
    image_size: str,
    aspect_ratio: str,
    quality: str,
    count: int,
    output_format: str,
    references: list[str],
    mask: str | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    if not 1 <= len(prompt) <= 32_000:
        raise AsxError("Prompt must contain 1-32,000 characters", code="invalid_prompt")
    if not quality.strip():
        raise AsxError("Quality cannot be empty", code="invalid_quality")
    if not 1 <= count <= 4:
        raise AsxError("Count must be between 1 and 4", code="invalid_count")
    if not 0 <= len(references) <= MAX_IMAGE_INPUTS:
        raise AsxError(
            f"At most {MAX_IMAGE_INPUTS} input images are supported",
            code="too_many_images",
        )
    if task_type == "image.edit" and not references:
        raise AsxError("Image editing requires at least one input image", code="missing_edit_image")

    models, request_id = client.models()
    model = resolve_model(models, model_selector, task_type)
    model_capabilities = capabilities(model)
    canonical_size = catalog_choice(
        image_size, model_capabilities.get("image_sizes"), "image_size"
    )
    canonical_ratio = catalog_choice(
        aspect_ratio, model_capabilities.get("aspect_ratios"), "aspect_ratio"
    )
    canonical_format = catalog_choice(
        output_format, model_capabilities.get("output_formats"), "output_format"
    )
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
        raise AsxError("Selected model does not support masks", code="unsupported_model_capability")

    prepared = [image_source(source) for source in references]
    task_input: dict[str, Any] = {
        "prompt": prompt,
        "count": count,
        "image_size": canonical_size,
        "aspect_ratio": canonical_ratio,
        "quality": quality,
        "output_format": canonical_format,
        "extra": {},
    }
    if task_type == "image.generate":
        task_input["reference_images"] = prepared
    else:
        task_input["images"] = prepared
        task_input["mask"] = image_source(mask) if mask else None
    body = {
        "task_type": task_type,
        "model": model["name"],
        "input": task_input,
        "asset_delivery": "asynx",
    }
    return body, str(model["name"]), request_id
