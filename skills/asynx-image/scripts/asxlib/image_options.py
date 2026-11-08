"""Explicit image options; never accept task identity or transport settings from JSON."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import AsxError

IMAGE_OPTIONS = {"size", "complexity", "background", "output_compression", "moderation",
                 "safety_identifier", "markup", "frame_count", "frame_duration_ms", "svg"}


def options_from_args(args: Any) -> dict[str, Any]:
    options: dict[str, Any] = {}
    path = getattr(args, "params_file", None)
    if path:
        try:
            source = Path(path).expanduser()
            if source.stat().st_size > 65536:
                raise ValueError("large parameter file")
            options = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise AsxError("参数文件须为不超过 64 KiB 的有效 JSON 对象。", code="invalid_image_options") from exc
        if not isinstance(options, dict) or set(options) - IMAGE_OPTIONS:
            raise AsxError("参数文件包含未知字段；仅支持图片高级参数，不能覆盖任务、认证或服务地址。", code="invalid_image_options")
    for key in IMAGE_OPTIONS - {"svg"}:
        value = getattr(args, key, None)
        if value is not None:
            options[key] = value
    svg_model = getattr(args, "svg_model", None)
    svg_effort = getattr(args, "svg_reasoning_effort", None)
    if svg_model is not None or svg_effort is not None:
        stored = options.get("svg", {})
        if not isinstance(stored, dict):
            raise AsxError("SVG 参数须为 JSON 对象。", code="invalid_image_options")
        svg = dict(stored)
        if svg_model is not None:
            svg["model"] = svg_model
        if svg_effort is not None:
            reasoning = svg.get("reasoning", {})
            if not isinstance(reasoning, dict):
                raise AsxError("SVG reasoning 参数须为 JSON 对象。", code="invalid_image_options")
            svg["reasoning"] = {**reasoning, "effort": svg_effort}
        options["svg"] = svg
    validate_image_options(options)
    if "size" in options and any(getattr(args, key, None) is not None for key in ("image_size", "aspect_ratio")):
        raise AsxError("--size 与 --image-size/--aspect-ratio 不能同时指定。", code="conflicting_image_options")
    return options


def validate_image_options(options: dict[str, Any]) -> None:
    for key, value in options.items():
        if key in {"output_compression", "frame_count", "frame_duration_ms"}:
            lower, upper = {"output_compression": (0, 100), "frame_count": (2, 64), "frame_duration_ms": (20, 2000)}[key]
            valid = type(value) is int and lower <= value <= upper
        elif key == "svg":
            # Keep partial settings until the request is built, so batch overrides
            # can retain an explicitly selected author before applying defaults.
            valid = isinstance(value, dict) and set(value) <= {"model", "reasoning"}
            if valid:
                if "model" in value:
                    model = value["model"]
                    valid = isinstance(model, str) and bool(model.strip()) and len(model) <= 160
                if "reasoning" in value:
                    reasoning = value["reasoning"]
                    valid = valid and isinstance(reasoning, dict) and set(reasoning) <= {"effort"}
                    if valid and "effort" in reasoning:
                        effort = reasoning["effort"]
                        valid = isinstance(effort, str) and bool(effort.strip()) and len(effort) <= 64
        else:
            valid = isinstance(value, str) and bool(value.strip())
        if not valid:
            raise AsxError("图片高级参数的类型或取值无效：" + key, code="invalid_image_options")
    if "size" in options and not re.fullmatch(r"auto|[1-9]\d*x[1-9]\d*", options["size"]):
        raise AsxError("size 须为 auto 或 WIDTHxHEIGHT。", code="invalid_image_options")


def resolved_image_options(
    output_format: str,
    options: dict[str, Any],
    *,
    image_size: str | None,
    aspect_ratio: str | None,
) -> dict[str, Any]:
    """Apply client resource bounds and GIF sizing after batch inheritance."""
    options = dict(options)
    validate_image_options(options)
    if output_format != "gif" and any(key in options for key in ("frame_count", "frame_duration_ms")):
        raise AsxError("--frame-count 与 --frame-duration-ms 仅适用于 GIF 输出。", code="invalid_image_options")
    if output_format == "gif":
        if options.get("size") is None and image_size is None and aspect_ratio is None:
            # A GIF's frame budget differs from static images; do not let the
            # service turn omitted dimensions into a static 1K square request.
            options["size"] = "auto"
    return options
