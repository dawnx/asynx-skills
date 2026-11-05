"""Read downloaded image metadata for delivery without another Agent tool call."""

from __future__ import annotations

import importlib
import math
import re
import warnings
from pathlib import Path
from typing import Any


def requested_aspect_ratio(body: Any) -> str | None:
    task_input = body.get("input") if isinstance(body, dict) else None
    if isinstance(task_input, dict) and task_input.get("size"):
        match = re.fullmatch(r"([1-9]\d*)x([1-9]\d*)", task_input["size"])
        return ":".join(match.groups()) if match else None
    ratio = task_input.get("aspect_ratio") if isinstance(task_input, dict) else None
    return ratio if isinstance(ratio, str) else None


def attach_image_details(
    payload: dict[str, Any], files: list[str], *, expected_ratio: str | None = None
) -> None:
    """Keep downloads deliverable even when optional metadata cannot be read."""
    details: list[dict[str, Any]] = []
    notices: list[dict[str, Any]] = []
    expected = re.fullmatch(r"([1-9]\d{0,4}):([1-9]\d{0,4})", expected_ratio or "")
    for value in files:
        path = Path(value)
        if path.suffix.lower() == ".svg":
            from .svg_results import svg_details
            from .errors import AsxError
            try:
                details.append({"path": value, "size_bytes": path.stat().st_size, **svg_details(path.read_bytes())})
            except (OSError, AsxError):
                notices.append({"code": "image_metadata_unavailable", "path": value, "message": "无法读取 SVG 元数据，文件路径已保留。"})
            continue
        try:
            image_module = importlib.import_module("PIL.Image")
            with warnings.catch_warnings():
                warnings.simplefilter("error", image_module.DecompressionBombWarning)
                with image_module.open(path) as source:
                    width, height = source.size
                    # Respect display orientation without modifying the downloaded file.
                    if source.getexif().get(274) in {5, 6, 7, 8}:
                        width, height = height, width
                    image_format = str(source.format or "").lower()
                    frame_count = int(getattr(source, "n_frames", 1))
                size_bytes = path.stat().st_size
            if width <= 0 or height <= 0:
                raise ValueError("invalid image dimensions")
        except (ImportError, OSError, EOFError, SyntaxError, ValueError, Warning):
            notices.append({
                "code": "image_metadata_unavailable", "path": value,
                "message": "无法读取结果图片的尺寸和格式，文件路径已保留；请检查图片是否可正常打开。",
            })
            continue
        except image_module.DecompressionBombError:
            notices.append({
                "code": "image_metadata_unavailable", "path": value,
                "message": "结果图片像素过多，未读取尺寸和格式；文件路径已保留，请使用图片查看工具检查。",
            })
            continue
        divisor = math.gcd(width, height)
        actual_ratio = f"{width // divisor}:{height // divisor}"
        details.append({
            "path": value, "width": width, "height": height, "format": image_format,
            "aspect_ratio": actual_ratio, "size_bytes": size_bytes, "frame_count": frame_count,
        })
        if expected:
            ratio_width, ratio_height = map(int, expected.groups())
            # Allow one-pixel rounding, but not a >1% ratio deviation on tiny images.
            difference = abs(width * ratio_height - height * ratio_width)
            if difference > max(ratio_width, ratio_height) or difference * 100 > height * ratio_width:
                notices.append({
                    "code": "aspect_ratio_mismatch", "path": value,
                    "requested_aspect_ratio": expected_ratio,
                    "actual_aspect_ratio": actual_ratio,
                    "message": f"结果图片为 {width}×{height}，与请求的 {expected_ratio} 画幅比例不符。",
                })
    payload["images"] = details
    if notices:
        payload.setdefault("warnings", []).extend(notices)
