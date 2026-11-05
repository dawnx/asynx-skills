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
    for key, value in options.items():
        if key in {"output_compression", "frame_count", "frame_duration_ms"}:
            lower, upper = {"output_compression": (0, 100), "frame_count": (2, 64), "frame_duration_ms": (20, 2000)}[key]
            valid = type(value) is int and lower <= value <= upper
        elif key == "svg":
            valid = isinstance(value, dict) and set(value) <= {"model", "reasoning"} and isinstance(value.get("model"), str) and bool(value["model"].strip())
        else:
            valid = isinstance(value, str) and bool(value.strip())
        if not valid:
            raise AsxError("图片高级参数的类型或取值无效：" + key, code="invalid_image_options")
    if "size" in options and not re.fullmatch(r"auto|[1-9]\d*x[1-9]\d*", options["size"]):
        raise AsxError("size 须为 auto 或 WIDTHxHEIGHT。", code="invalid_image_options")
    if "size" in options and any(getattr(args, key, None) is not None for key in ("image_size", "aspect_ratio")):
        raise AsxError("--size 与 --image-size/--aspect-ratio 不能同时指定。", code="conflicting_image_options")
    return options
