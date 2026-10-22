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
                size_bytes = path.stat().st_size
            if width <= 0 or height <= 0:
                raise ValueError("invalid image dimensions")
        except (ImportError, OSError, EOFError, SyntaxError, ValueError, Warning):
            notices.append({
                "code": "image_metadata_unavailable", "path": value,
                "message": "无法读取结果图片的尺寸和格式，请检查该文件；任务记录和下载路径已保留。",
            })
            continue
        except image_module.DecompressionBombError:
            notices.append({
                "code": "image_metadata_unavailable", "path": value,
                "message": "结果图片超过安全像素限制，未读取尺寸和格式；任务记录和下载路径已保留。",
            })
            continue
        divisor = math.gcd(width, height)
        actual_ratio = f"{width // divisor}:{height // divisor}"
        details.append({
            "path": value, "width": width, "height": height, "format": image_format,
            "aspect_ratio": actual_ratio, "size_bytes": size_bytes,
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
                    "message": f"结果图片为 {width}×{height}，与请求的 {expected_ratio} 比例不符。",
                })
    payload["images"] = details
    if notices:
        payload.setdefault("warnings", []).extend(notices)
