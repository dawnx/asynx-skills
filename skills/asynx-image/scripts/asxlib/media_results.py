"""Media signatures and delivery metadata without video or 3D decoding dependencies."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from .errors import AsxError
from .image_results import attach_image_details
from .svg_results import is_svg, svg_details

# 3D result types the service delivers and the extension each is saved under.
# An archive is kept as delivered and never unpacked.
MODEL_FILE_EXTENSIONS = {
    "model/gltf-binary": "glb",
    "application/vnd.autodesk.fbx": "fbx",
    "model/gltf+json": "gltf",
    "model/obj": "obj",
    "model/stl": "stl",
    "model/vnd.usdz+zip": "usdz",
    "model/3mf": "3mf",
    "application/zip": "zip",
}
# A ZIP end record is 22 bytes plus a comment of at most 65535.
_ZIP_TAIL_BYTES = 65_535 + 22


def image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if is_svg(data):
        svg_details(data)
        return "image/svg+xml"
    raise AsxError("结果需为 PNG、JPEG、WebP、GIF 或 SVG。", code="unsupported_image_format")


def model_content_type(value: Any) -> str | None:
    """The 3D result type a declared content type names, if it is one the service delivers."""
    normalized = value.split(";", 1)[0].strip().lower() if isinstance(value, str) else ""
    return normalized if normalized in MODEL_FILE_EXTENSIONS else None


def incomplete_model_file(detail: str) -> AsxError:
    return AsxError(
        f"3D 模型文件不完整：{detail}请通过 wait 重新下载原任务，无需重新生成或重新提交。",
        code="incomplete_model_file",
    )


def media_file_info(path: Path, *, model_content_type_hint: str | None = None) -> dict[str, Any]:
    """Identify supported containers by bytes, not by extension or HTTP MIME.

    3D formats other than GLB have weak signatures or none (OBJ and STL are plain
    data, USDZ and 3MF are both ZIP), so those are checked against the type the
    service declared for the asset, or against the extension a saved file got from it.
    """
    with path.open("rb") as source:
        header = source.read(64 * 1024)
        size = path.stat().st_size
        digest = hashlib.sha256(header)
        while chunk := source.read(64 * 1024):
            digest.update(chunk)
        source.seek(max(0, size - _ZIP_TAIL_BYTES))
        tail = source.read()
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        kind, content_type, extension = "image", "image/png", "png"
    elif header.startswith(b"\xff\xd8\xff"):
        kind, content_type, extension = "image", "image/jpeg", "jpg"
    elif len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        kind, content_type, extension = "image", "image/webp", "webp"
    elif header.startswith((b"GIF87a", b"GIF89a")):
        kind, content_type, extension = "image", "image/gif", "gif"
    elif is_svg(header):
        svg_details(path.read_bytes())
        kind, content_type, extension = "image", "image/svg+xml", "svg"
    elif _is_mp4(header, size):
        kind, content_type, extension = "video", "video/mp4", "mp4"
    elif header[:4] == b"glTF":
        # Binary glTF declares its total length at bytes 8..12; anything else is a partial file.
        if len(header) < 12 or int.from_bytes(header[8:12], "little") != size:
            raise incomplete_model_file("GLB 文件头声明的长度与实际大小不符。")
        kind, content_type, extension = "model", "model/gltf-binary", "glb"
    else:
        declared = model_content_type(model_content_type_hint) or next(
            (name for name, suffix in MODEL_FILE_EXTENSIONS.items() if path.suffix.lower() == f".{suffix}"), None
        )
        if declared is None or not _is_model_container(declared, header, tail, size):
            raise AsxError(
                "无法识别结果文件格式，目前支持 PNG、JPEG、WebP、GIF、SVG 图片、MP4 视频，以及 GLB、FBX 等 3D 模型文件。",
                code="unsupported_media_format",
            )
        kind, content_type, extension = "model", declared, MODEL_FILE_EXTENSIONS[declared]
    return {
        "kind": kind, "content_type": content_type, "extension": extension,
        "size_bytes": size, "sha256": digest.hexdigest(),
    }


def _is_model_container(content_type: str, header: bytes, tail: bytes, size: int) -> bool:
    """A cheap signature check per declared 3D type.

    Enough to refuse an error page, another format or a cut download; the
    service validated the file it stored, and scenes are never parsed here.
    """
    extension = MODEL_FILE_EXTENSIONS[content_type]
    if extension == "fbx":
        return header.startswith((b"Kaydara FBX Binary  \x00", b"; FBX"))
    if extension in {"usdz", "3mf", "zip"}:
        if not header.startswith(b"PK\x03\x04"):
            return False
        if not _zip_is_complete(tail, size):
            raise incomplete_model_file("压缩包缺少完整的结尾记录。")
        return True
    if extension == "stl":
        # Binary: 80-byte header, triangle count, 50 bytes per triangle. Otherwise ASCII.
        if size >= 84 and 84 + 50 * int.from_bytes(header[80:84], "little") == size:
            return True
        return header.lstrip().startswith(b"solid")
    if b"\x00" in header:
        return False
    text = header.decode("utf-8", errors="ignore").lstrip("\ufeff").lstrip()
    if extension == "gltf":
        if not text.startswith("{") or '"asset"' not in text:
            return False
        if not tail.rstrip().endswith(b"}"):
            raise incomplete_model_file("glTF 文本没有完整结束。")
        return True
    if extension == "obj":
        return re.search(r"(?m)^[ \t]*(?:v|f)[ \t]", text) is not None
    return False


def _zip_is_complete(tail: bytes, size: int) -> bool:
    """Whether the archive ends with a whole end-of-central-directory record."""
    record = tail.rfind(b"PK\x05\x06")
    if record < 0 or record + 22 > len(tail):
        return False
    directory_size = int.from_bytes(tail[record + 12:record + 16], "little")
    directory_offset = int.from_bytes(tail[record + 16:record + 20], "little")
    comment_size = int.from_bytes(tail[record + 20:record + 22], "little")
    # The record states its own comment length and where the directory lies.
    return (
        record + 22 + comment_size == len(tail)
        and directory_offset + directory_size <= size - len(tail) + record
    )


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
    """Expose video and 3D files without presenting requested properties as measured facts."""
    image_paths: list[str] = []
    media: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    models: list[dict[str, Any]] = []
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
        elif info["kind"] == "model":
            models.append({**details, "format": info["extension"]})
        else:
            videos.append({**details, "format": info["extension"]})
    # A poster or preview belongs to the same task but need not share its aspect ratio.
    attach_image_details(payload, image_paths, expected_ratio=None if videos or models else expected_ratio)
    payload["videos"] = videos
    if models or str((task or {}).get("task_type", "")).startswith("model."):
        payload["models"] = models
    payload["media"] = media
