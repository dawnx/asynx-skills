from __future__ import annotations

import base64
import binascii
import importlib
import io
import json
import math
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .constants import (
    MAX_MASK_BYTES,
    MAX_REFERENCE_BYTES,
    MAX_REFERENCE_DIMENSION,
    MAX_REFERENCE_IMAGES,
    MAX_REFERENCE_PIXELS,
    MAX_REFERENCE_SOURCE_BYTES,
    MAX_REFERENCE_TOTAL_BYTES,
    MAX_REQUEST_BODY_BYTES,
    MAX_SAFE_IMAGE_PIXELS,
    REFERENCE_WEBP_QUALITY,
)
from .errors import AsxError

_DATA_URL = re.compile(
    r"\Adata:(?P<mime>image/[A-Za-z0-9][A-Za-z0-9.+-]*);base64,(?P<data>.+)\Z",
    re.IGNORECASE | re.DOTALL,
)
_SUPPORTED_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_MAX_BASE64_SOURCE_CHARACTERS = ((MAX_REFERENCE_SOURCE_BYTES + 2) // 3) * 4


@dataclass(frozen=True, slots=True)
class PreparedImage:
    source: str
    content_type: str | None
    size_bytes: int | None
    width: int | None
    height: int | None
    normalized: bool


def _pillow() -> tuple[Any, Any, Any]:
    try:
        image_module: Any = importlib.import_module("PIL.Image")
        image_ops: Any = importlib.import_module("PIL.ImageOps")
        unidentified = importlib.import_module("PIL").UnidentifiedImageError
    except (ImportError, AttributeError) as exc:
        raise AsxError(
            "参考图处理需要 Pillow；请重新运行 install.py 安装运行依赖",
            code="pillow_not_installed",
        ) from exc
    image_module.MAX_IMAGE_PIXELS = MAX_SAFE_IMAGE_PIXELS
    return image_module, image_ops, unidentified


def _validated_remote_url(value: str) -> str:
    if value != value.strip() or len(value) > 2048 or any(char.isspace() for char in value):
        raise AsxError("Image URL is invalid", code="invalid_image_url")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise AsxError("Image URL is invalid", code="invalid_image_url") from exc
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise AsxError("Image URL is invalid", code="invalid_image_url")
    return value


def _read_source(value: str, *, max_bytes: int) -> tuple[bytes, str | None]:
    match = _DATA_URL.fullmatch(value)
    if match is not None:
        encoded = match.group("data")
        max_characters = ((max_bytes + 2) // 3) * 4
        if len(encoded) > min(_MAX_BASE64_SOURCE_CHARACTERS, max_characters + 4):
            raise AsxError("Image exceeds the input size limit", code="reference_image_too_large")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AsxError("Image Data URL is invalid", code="invalid_base64_image") from exc
        if not data or len(data) > max_bytes:
            raise AsxError("Image exceeds the input size limit", code="reference_image_too_large")
        return data, match.group("mime").lower()

    path = Path(value).expanduser()
    if not path.is_file():
        raise AsxError(f"Image file does not exist: {path}", code="image_not_found")
    try:
        size = path.stat().st_size
        if size <= 0 or size > max_bytes:
            raise AsxError("Image exceeds the input size limit", code="reference_image_too_large")
        return path.read_bytes(), None
    except AsxError:
        raise
    except OSError as exc:
        raise AsxError(f"Cannot read image file {path}: {exc}", code="image_read_failed") from exc


def _decoded_image(data: bytes) -> tuple[Any, str, int]:
    image_module, image_ops, unidentified = _pillow()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", image_module.DecompressionBombWarning)
            with image_module.open(io.BytesIO(data)) as verified:
                verified.verify()
            with image_module.open(io.BytesIO(data)) as source:
                frame_count = int(getattr(source, "n_frames", 1))
                if frame_count != 1:
                    raise AsxError(
                        "Animated or multi-frame images are not supported",
                        code="animated_image_not_supported",
                    )
                source.load()
                content_type = _SUPPORTED_FORMATS.get((source.format or "").upper())
                if content_type is None:
                    raise AsxError(
                        "Only PNG, JPEG, and WebP images are supported",
                        code="unsupported_image_format",
                    )
                oriented = image_ops.exif_transpose(source)
                oriented.load()
                image = oriented.copy()
    except AsxError:
        raise
    except (image_module.DecompressionBombError, image_module.DecompressionBombWarning) as exc:
        raise AsxError("Image resolution is too large", code="image_pixel_limit_exceeded") from exc
    except (unidentified, EOFError, OSError, SyntaxError, ValueError) as exc:
        raise AsxError("Image file is invalid", code="invalid_image_content") from exc
    width, height = image.size
    if width <= 0 or height <= 0 or width * height > MAX_SAFE_IMAGE_PIXELS:
        raise AsxError("Image resolution is too large", code="image_pixel_limit_exceeded")
    return image, content_type, frame_count


def _data_url(content_type: str, data: bytes) -> str:
    return f"data:{content_type};base64,{base64.b64encode(data).decode('ascii')}"


def _webp(image: Any) -> tuple[bytes, int, int]:
    image_module, _image_ops, _unidentified = _pillow()
    width, height = image.size
    scale = min(
        1.0,
        MAX_REFERENCE_DIMENSION / max(width, height),
        math.sqrt(MAX_REFERENCE_PIXELS / (width * height)),
    )
    target_width = max(1, round(width * scale))
    target_height = max(1, round(height * scale))
    prepared = (
        image.resize((target_width, target_height), image_module.Resampling.LANCZOS)
        if (target_width, target_height) != (width, height)
        else image.copy()
    )
    has_alpha = "A" in prepared.getbands() or "transparency" in prepared.info
    if prepared.mode not in {"RGB", "RGBA"}:
        prepared = prepared.convert("RGBA" if has_alpha else "RGB")
    output = io.BytesIO()
    prepared.save(output, format="WEBP", quality=REFERENCE_WEBP_QUALITY, method=4)
    data = output.getvalue()
    if len(data) > MAX_REFERENCE_BYTES:
        raise AsxError(
            "Image remains larger than 5 MB after one-pass normalization",
            code="reference_image_normalization_failed",
        )
    return data, target_width, target_height


def prepare_image(value: str, *, keep_original: bool = False) -> PreparedImage:
    if value.casefold().startswith(("http://", "https://")):
        return PreparedImage(_validated_remote_url(value), None, None, None, None, False)
    data, _declared_type = _read_source(value, max_bytes=MAX_REFERENCE_SOURCE_BYTES)
    image, content_type, _frame_count = _decoded_image(data)
    width, height = image.size
    if keep_original:
        if (
            len(data) > MAX_REFERENCE_BYTES
            or max(width, height) > MAX_REFERENCE_DIMENSION
            or width * height > MAX_REFERENCE_PIXELS
        ):
            raise AsxError(
                "Original image exceeds the reference delivery limits; remove --keep-reference-original",
                code="reference_image_requires_normalization",
            )
        return PreparedImage(_data_url(content_type, data), content_type, len(data), width, height, False)
    normalized, width, height = _webp(image)
    return PreparedImage(
        _data_url("image/webp", normalized),
        "image/webp",
        len(normalized),
        width,
        height,
        True,
    )


def prepare_mask(value: str, *, source: PreparedImage | None) -> PreparedImage:
    if value.casefold().startswith(("http://", "https://")):
        return PreparedImage(_validated_remote_url(value), None, None, None, None, False)
    data, _declared_type = _read_source(value, max_bytes=MAX_MASK_BYTES)
    image, content_type, _frame_count = _decoded_image(data)
    if content_type != "image/png":
        raise AsxError("Image edit mask must be PNG", code="invalid_image_mask")
    width, height = image.size
    if (
        len(data) > MAX_MASK_BYTES
        or max(width, height) > MAX_REFERENCE_DIMENSION
        or width * height > MAX_REFERENCE_PIXELS
    ):
        raise AsxError("Image edit mask exceeds the size limits", code="invalid_image_mask")
    if "A" not in image.getbands() and "transparency" not in image.info:
        raise AsxError("Image edit mask must contain alpha", code="invalid_image_mask")
    alpha = image.convert("RGBA").getchannel("A")
    minimum, maximum = alpha.getextrema()
    if minimum != 0 or maximum != 255:
        raise AsxError(
            "Image edit mask must contain transparent and opaque regions",
            code="invalid_image_mask",
        )
    if source and source.width and source.height:
        if width > source.width or height > source.height:
            raise AsxError("Image edit mask cannot be larger than the source", code="invalid_image_mask")
        aspect_delta = abs(width * source.height - height * source.width)
        if aspect_delta > max(source.width, source.height):
            raise AsxError("Image edit mask aspect ratio must match the source", code="invalid_image_mask")
    return PreparedImage(_data_url("image/png", data), "image/png", len(data), width, height, False)


def prepare_inputs(
    references: list[str],
    *,
    mask: str | None,
    keep_original: bool,
) -> tuple[list[str], str | None]:
    if len(references) > MAX_REFERENCE_IMAGES:
        raise AsxError(
            f"At most {MAX_REFERENCE_IMAGES} input images are supported",
            code="too_many_images",
        )
    prepared = [prepare_image(value, keep_original=keep_original) for value in references]
    prepared_mask = prepare_mask(mask, source=prepared[0] if prepared else None) if mask else None
    total_bytes = sum(item.size_bytes or 0 for item in prepared)
    if prepared_mask:
        total_bytes += prepared_mask.size_bytes or 0
    if total_bytes > MAX_REFERENCE_TOTAL_BYTES:
        raise AsxError(
            "Combined local image inputs exceed 20 MB",
            code="reference_images_total_too_large",
        )
    return [item.source for item in prepared], prepared_mask.source if prepared_mask else None


def request_body_size(body: dict[str, Any]) -> int:
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def validate_request_body(body: dict[str, Any]) -> None:
    size = request_body_size(body)
    if size > MAX_REQUEST_BODY_BYTES:
        raise AsxError("Task request body exceeds 32 MB", code="request_body_too_large")
