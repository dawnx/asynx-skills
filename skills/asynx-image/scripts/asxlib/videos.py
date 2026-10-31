"""MiniMax-H3 requests use the public video contract, never provider fields."""
from __future__ import annotations

import argparse
import io
import math
from typing import Any

from .constants import MAX_REFERENCE_SOURCE_BYTES
from .errors import AsxError
from .reference_media import (
    _data_url,
    _decoded_image,
    _pillow,
    _read_source,
    _validated_remote_url,
    validate_request_body,
)

VIDEO_MODEL = "MiniMax-H3"


def prepare_video_image(value: str) -> str:
    if value.casefold().startswith(("http://", "https://")):
        return _validated_remote_url(value)
    data, _mime = _read_source(value, max_bytes=MAX_REFERENCE_SOURCE_BYTES)
    image, _content_type, _frames = _decoded_image(data)
    image_module, _ops, _unidentified = _pillow()
    width, height = image.size
    scale = min(1.0, 1600 / max(width, height))
    if scale < 1:
        image = image.resize((max(1, round(width * scale)), max(1, round(height * scale))),
                             image_module.Resampling.LANCZOS)
    alpha = "A" in image.getbands() or "transparency" in image.info
    image = image.convert("RGBA" if alpha else "RGB")
    output = io.BytesIO()
    image.save(output, format="PNG" if alpha else "JPEG", **({} if alpha else {"quality": 82}))
    prepared = output.getvalue()
    if len(prepared) > 3_000_000:
        raise AsxError("参考图压缩后仍超过 3 MB，请降低原图分辨率或提供较小的图片。", code="video_reference_too_large")
    return _data_url("image/png" if alpha else "image/jpeg", prepared)


def _media_url(value: str) -> str:
    if not value.casefold().startswith(("http://", "https://")):
        raise AsxError("视频和音频参考需要 HTTP(S) 链接，目前不能直接传入本地文件。", code="video_reference_url_required")
    return _validated_remote_url(value)


def build_video_task(
    client: Any, *, prompt: str, model_selector: str | None = None,
    duration: float = 5, resolution: str = "720p", aspect_ratio: str = "16:9",
    references: list[str] | None = None, first_frame: str | None = None,
    last_frame: str | None = None, reference_videos: list[str] | None = None,
    reference_audios: list[str] | None = None,
) -> tuple[dict[str, Any], str, None]:
    if model_selector is not None and model_selector.strip().casefold() != VIDEO_MODEL.casefold():
        raise AsxError("暂不支持所选视频模型，请使用默认模型（省略 --model）。", code="unsupported_video_model")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 5000:
        raise AsxError("视频描述须为 1–5000 个字符。", code="invalid_prompt")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration):
        raise AsxError("视频时长需填写有效的秒数，例如 5。", code="invalid_video_duration")
    resolutions = {"720p": "720p", "1080p": "1080p", "2k": "2K"}
    canonical_resolution = resolutions.get(resolution.strip().casefold())
    if canonical_resolution is None:
        raise AsxError("请选择支持的视频分辨率：720p、1080p 或 2K。", code="unsupported_video_resolution")
    if last_frame and not first_frame:
        raise AsxError("尾帧需要同时提供首帧。", code="missing_first_frame")
    inputs: list[dict[str, str]] = []
    for role, values in (("reference", references or []), ("first_frame", [first_frame] if first_frame else []),
                         ("last_frame", [last_frame] if last_frame else [])):
        inputs.extend({"type": "image", "role": role, "url": prepare_video_image(value)} for value in values)
    for kind, values in (("video", reference_videos or []), ("audio", reference_audios or [])):
        inputs.extend({"type": kind, "role": "reference", "url": _media_url(value)} for value in values)
    body = {"task_type": "video.generate", "model": VIDEO_MODEL, "asset_delivery": "asynx",
            "input": {"prompt": prompt, "duration": duration, "resolution": canonical_resolution,
                      "aspect_ratio": aspect_ratio, "inputs": inputs, "extra": {}}}
    validate_request_body(body)
    return body, VIDEO_MODEL, None


def body_from_video_args(client: Any, args: argparse.Namespace) -> tuple[dict[str, Any], str, None]:
    return build_video_task(
        client, prompt=args.prompt, model_selector=getattr(args, "model", None),
        duration=args.duration if getattr(args, "duration", None) is not None else 5,
        resolution=args.resolution if getattr(args, "resolution", None) is not None else "720p",
        aspect_ratio=getattr(args, "aspect_ratio", None) or "16:9",
        references=getattr(args, "reference", []), first_frame=getattr(args, "first_frame", None),
        last_frame=getattr(args, "last_frame", None), reference_videos=getattr(args, "reference_video", []),
        reference_audios=getattr(args, "reference_audio", []),
    )
