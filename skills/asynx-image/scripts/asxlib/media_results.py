"""Media signatures and delivery metadata without video decoding dependencies."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from .errors import AsxError
from .image_results import attach_image_details


def media_file_info(path: Path) -> dict[str, Any]:
    """Identify supported containers by bytes, not by extension or HTTP MIME."""
    with path.open("rb") as source:
        header = source.read(64 * 1024)
        size = path.stat().st_size
        digest = hashlib.sha256(header)
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        kind, content_type, extension = "image", "image/png", "png"
    elif header.startswith(b"\xff\xd8\xff"):
        kind, content_type, extension = "image", "image/jpeg", "jpg"
    elif len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        kind, content_type, extension = "image", "image/webp", "webp"
    elif _is_mp4(header, size):
        kind, content_type, extension = "video", "video/mp4", "mp4"
    else:
        raise AsxError("无法识别结果文件格式，目前支持 PNG、JPEG、WebP 图片和 MP4 视频。", code="unsupported_media_format")
    return {
        "kind": kind, "content_type": content_type, "extension": extension,
        "size_bytes": size, "sha256": digest.hexdigest(),
    }


def _is_mp4(header: bytes, file_size: int) -> bool:
    if len(header) < 24 or header[4:8] != b"ftyp":
        return False
    box_size = int.from_bytes(header[:4], "big")
    if box_size < 16 or box_size % 4 or box_size > len(header) or box_size >= file_size:
        return False
    brands = [header[8:12], *(header[index:index + 4] for index in range(16, box_size, 4))]
    # Exclude image-only and QuickTime containers which also have an ftyp box.
    if any(brand in {b"avif", b"avis", b"heic", b"heix", b"mif1", b"qt  "} for brand in brands):
        return False
    return any(brand in {b"isom", b"iso2", b"iso3", b"iso4", b"iso5", b"iso6", b"mp41", b"mp42", b"avc1", b"dash", b"M4V ", b"MSNV"} for brand in brands)


def attach_media_details(
    payload: dict[str, Any],
    files: list[str],
    *,
    expected_ratio: str | None = None,
    task: dict[str, Any] | None = None,
) -> None:
    """Expose video files without presenting requested properties as measured facts."""
    image_paths: list[str] = []
    media: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    for value in files:
        try:
            info = media_file_info(Path(value))
        except (OSError, AsxError):
            payload.setdefault("warnings", []).append({
                "code": "media_metadata_unavailable", "path": value,
                "message": "无法读取结果文件信息，下载路径已保留；请检查文件是否可正常打开。",
            })
            continue
        details = {"path": value, "kind": info["kind"], "content_type": info["content_type"], "size_bytes": info["size_bytes"]}
        media.append(details)
        if info["kind"] == "image":
            image_paths.append(value)
        else:
            videos.append({**details, "format": info["extension"]})
    # A poster belongs to the same video task but need not share its aspect ratio.
    attach_image_details(payload, image_paths, expected_ratio=None if videos else expected_ratio)
    payload["videos"] = videos
    payload["media"] = media
