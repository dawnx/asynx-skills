from __future__ import annotations

import argparse
import importlib
import math
from pathlib import Path
from typing import Any

from .constants import MAX_REFERENCE_SOURCE_BYTES, MAX_SAFE_IMAGE_PIXELS
from .errors import AsxError

_FORMATS = {
    "png": ("PNG", ".png"),
    "jpeg": ("JPEG", ".jpg"),
    "jpg": ("JPEG", ".jpg"),
    "webp": ("WEBP", ".webp"),
}
_INPUT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


def _pillow() -> tuple[Any, Any, Any, Any, Any]:
    try:
        image: Any = importlib.import_module("PIL.Image")
        image_chops: Any = importlib.import_module("PIL.ImageChops")
        image_color: Any = importlib.import_module("PIL.ImageColor")
        image_ops: Any = importlib.import_module("PIL.ImageOps")
        unidentified = importlib.import_module("PIL").UnidentifiedImageError
    except (ImportError, AttributeError) as exc:
        raise AsxError(
            "缺少图片处理依赖 Pillow，请按安装说明安装依赖后重试。",
            code="pillow_not_installed",
        ) from exc
    image.MAX_IMAGE_PIXELS = MAX_SAFE_IMAGE_PIXELS
    return image, image_chops, image_color, image_ops, unidentified


def _input_path(value: str) -> Path:
    if value.casefold().startswith(("http://", "https://", "data:")):
        raise AsxError("本地图片处理需要文件路径，请先将图片保存到本机。", code="local_image_requires_path")
    path = Path(value).expanduser()
    try:
        if not path.is_file():
            raise AsxError(f"图片文件不存在：{path}", code="image_not_found")
        if path.stat().st_size <= 0 or path.stat().st_size > MAX_REFERENCE_SOURCE_BYTES:
            raise AsxError("图片文件为空或超过 25 MiB，请使用较小的有效图片。", code="image_too_large")
    except AsxError:
        raise
    except OSError as exc:
        raise AsxError(f"图片读取失败，请检查文件是否可访问：{path}", code="image_read_failed") from exc
    return path.resolve()


def _open_image(value: str, *, allow_animated: bool = False) -> tuple[Any, Path, str, int]:
    image, _chops, _color, image_ops, unidentified = _pillow()
    path = _input_path(value)
    try:
        with image.open(path) as source:
            source_format = str(source.format or "").upper()
            if source_format not in {"PNG", "JPEG", "WEBP"}:
                raise AsxError(
                    "仅支持 PNG、JPEG 和 WebP 格式的图片。",
                    code="unsupported_image_format",
                )
            frames = int(getattr(source, "n_frames", 1))
            if frames != 1 and not allow_animated:
                raise AsxError("不支持动画或多帧图片，请选取一帧保存为静态图片。", code="animated_image_not_supported")
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_SAFE_IMAGE_PIXELS:
                raise AsxError("图片像素过多，无法处理；请降低分辨率后重试。", code="image_pixel_limit_exceeded")
            source.load()
            oriented = image_ops.exif_transpose(source)
            oriented.load()
            return oriented.copy(), path, source_format, frames
    except AsxError:
        raise
    except (image.DecompressionBombError, image.DecompressionBombWarning) as exc:
        raise AsxError("图片像素过多，无法处理；请降低分辨率后重试。", code="image_pixel_limit_exceeded") from exc
    except (unidentified, EOFError, OSError, SyntaxError, ValueError) as exc:
        raise AsxError(f"无法解析图片内容，请确认文件可正常打开，或重新导出：{path}", code="invalid_image_content") from exc


def _format(value: str | None, output: Path) -> tuple[str, str]:
    selected = (value or output.suffix.removeprefix(".") or "png").casefold()
    if selected not in _FORMATS:
        raise AsxError(
            f"不支持输出格式 {selected}，可选 png、jpeg 或 webp。",
            code="unsupported_output_format",
        )
    return _FORMATS[selected]


def _ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    except OSError as exc:
        raise AsxError(f"无法创建输出目录，请检查目录权限或更换位置：{path.parent}", code="output_directory_failed") from exc


def _save(
    image: Any, output: str, output_format: str | None = None, *, exclusive: bool = False
) -> dict[str, Any]:
    image_module, _chops, _color, _ops, _unidentified = _pillow()
    path = Path(output).expanduser().resolve()
    _ensure_parent(path)
    format_name, suffix = _format(output_format, path)
    if path.suffix.casefold() not in {f".{suffix.removeprefix('.')}", ".jpg" if suffix == ".jpg" else suffix}:
        path = path.with_suffix(suffix)
    prepared = image
    if format_name == "JPEG" and prepared.mode not in {"RGB", "L"}:
        rgba = prepared.convert("RGBA")
        background = image_module.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        prepared = background
    elif format_name in {"PNG", "WEBP"} and prepared.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
        prepared = prepared.convert("RGBA" if "A" in prepared.getbands() else "RGB")
    try:
        if exclusive:
            with path.open("xb") as handle:
                prepared.save(handle, format=format_name)
        else:
            prepared.save(path, format=format_name)
        size = path.stat().st_size
    except OSError as exc:
        raise AsxError(f"图片保存失败，请检查写入权限与磁盘空间：{path}", code="image_write_failed") from exc
    return {"path": str(path), "format": format_name.lower(), "width": image.width, "height": image.height, "size_bytes": size}


def _image_info(value: str) -> dict[str, Any]:
    image, path, source_format, frames = _open_image(value, allow_animated=True)
    has_alpha = "A" in image.getbands() or "transparency" in image.info
    return {
        "path": str(path),
        "format": source_format.lower(),
        "mode": image.mode,
        "width": image.width,
        "height": image.height,
        "frames": frames,
        "has_alpha": has_alpha,
        "size_bytes": path.stat().st_size,
    }


def _resize(value: str, output: str, width: int | None, height: int | None, output_format: str | None) -> dict[str, Any]:
    if width is None and height is None:
        raise AsxError("缩放图片时请指定宽度（--width）或高度（--height）。", code="invalid_resize")
    if width is not None and width <= 0 or height is not None and height <= 0:
        raise AsxError("缩放后的宽度和高度须为大于 0 的整数。", code="invalid_resize")
    image, _path, _format_name, _frames = _open_image(value)
    image_module, _chops, _color, _ops, _unidentified = _pillow()
    if width is None:
        width = max(1, round(image.width * height / image.height))
    elif height is None:
        height = max(1, round(image.height * width / image.width))
    assert width is not None and height is not None
    resized = image.resize((width, height), image_module.Resampling.LANCZOS)
    result = _save(resized, output, output_format)
    result["source"] = str(_input_path(value))
    return result


def _parse_box(value: str) -> tuple[int, int, int, int]:
    try:
        parts = [int(item.strip()) for item in value.split(",")]
    except ValueError as exc:
        raise AsxError("请按左、上、右、下的顺序填写四个裁剪坐标，以英文逗号分隔。", code="invalid_crop_box") from exc
    if len(parts) != 4 or parts[2] <= parts[0] or parts[3] <= parts[1] or min(parts) < 0:
        raise AsxError("裁剪坐标不能为负数，且右边界须大于左边界、下边界须大于上边界。", code="invalid_crop_box")
    return parts[0], parts[1], parts[2], parts[3]


def _crop(value: str, output: str, box_value: str, output_format: str | None) -> dict[str, Any]:
    image, _path, _format_name, _frames = _open_image(value)
    left, top, right, bottom = _parse_box(box_value)
    if right > image.width or bottom > image.height:
        raise AsxError("裁剪范围超出图片边界，请检查原图尺寸并调整坐标。", code="invalid_crop_box")
    result_image = image.crop((left, top, right, bottom))
    result = _save(result_image, output, output_format)
    result["source"] = str(_input_path(value))
    result["box"] = [left, top, right, bottom]
    return result


def _slice(value: str, output_dir: str, rows: int, columns: int, prefix: str, output_format: str | None) -> dict[str, Any]:
    if rows <= 0 or columns <= 0:
        raise AsxError("切图的行数和列数须为大于 0 的整数。", code="invalid_slice_grid")
    image, source, _format_name, _frames = _open_image(value)
    output_path = Path(output_dir).expanduser().resolve()
    _ensure_parent(output_path / "placeholder")
    files: list[dict[str, Any]] = []
    for row in range(rows):
        top = row * image.height // rows
        bottom = (row + 1) * image.height // rows
        for column in range(columns):
            left = column * image.width // columns
            right = (column + 1) * image.width // columns
            tile = image.crop((left, top, right, bottom))
            filename = output_path / f"{prefix}-{row + 1:02d}-{column + 1:02d}"
            files.append(_save(tile, str(filename), output_format))
    return {"source": str(source), "rows": rows, "columns": columns, "files": files, "count": len(files)}


def _contact_sheet(values: list[str], output: str, columns: int, cell_width: int, cell_height: int, background: str, output_format: str | None) -> dict[str, Any]:
    if not values:
        raise AsxError("制作预览拼图至少需要一张图片，请提供输入图片。", code="missing_images")
    if columns <= 0 or cell_width <= 0 or cell_height <= 0:
        raise AsxError("预览拼图的列数及每格宽高须为大于 0 的整数。", code="invalid_contact_sheet")
    image_module, _chops, image_color, _ops, _unidentified = _pillow()
    images = [_open_image(value)[0] for value in values]
    try:
        fill = image_color.getrgb(background)
    except ValueError as exc:
        raise AsxError("预览拼图的背景色无效，请使用颜色名称或十六进制色值（例如 #ffffff）。", code="invalid_contact_sheet") from exc
    rows = math.ceil(len(images) / columns)
    sheet = image_module.new("RGB", (columns * cell_width, rows * cell_height), fill)
    for index, source in enumerate(images):
        thumb = source.convert("RGB")
        thumb.thumbnail((cell_width, cell_height), image_module.Resampling.LANCZOS)
        left = (index % columns) * cell_width + (cell_width - thumb.width) // 2
        top = (index // columns) * cell_height + (cell_height - thumb.height) // 2
        sheet.paste(thumb, (left, top))
    result = _save(sheet, output, output_format)
    result["count"] = len(images)
    result["columns"] = columns
    result["rows"] = rows
    return result


def _apply_mask(value: str, mask_value: str, output: str, output_format: str | None) -> dict[str, Any]:
    _image_module, image_chops, _color, _ops, _unidentified = _pillow()
    image, source, _format_name, _frames = _open_image(value)
    mask, mask_path, _mask_format, _mask_frames = _open_image(mask_value)
    if mask.size != image.size:
        raise AsxError("蒙版（Mask）的宽高须与原图一致，请调整蒙版尺寸。", code="invalid_image_mask")
    if "A" in mask.getbands() or "transparency" in mask.info:
        alpha = mask.convert("RGBA").getchannel("A")
    elif mask.mode in {"1", "L"}:
        alpha = mask.convert("L")
    else:
        raise AsxError("蒙版（Mask）须为带透明通道的图片或灰度图片，请重新导出。", code="invalid_image_mask")
    result_image = image.convert("RGBA")
    existing = result_image.getchannel("A")
    result_image.putalpha(image_chops.multiply(existing, alpha))
    result = _save(result_image, output, output_format or "png")
    result["source"] = str(source)
    result["mask"] = str(mask_path)
    return result


def _batch_convert(input_dir: str, output_dir: str, output_format: str, recursive: bool) -> dict[str, Any]:
    source_dir = Path(input_dir).expanduser().resolve()
    if not source_dir.is_dir():
        raise AsxError(f"输入目录不存在：{source_dir}", code="image_directory_not_found")
    destination = Path(output_dir).expanduser().resolve()
    pattern = "**/*" if recursive else "*"
    sources = sorted(path for path in source_dir.glob(pattern) if path.is_file() and path.suffix.casefold() in _INPUT_SUFFIXES)
    if not sources:
        raise AsxError("输入目录中没有 PNG、JPEG 或 WebP 图片。", code="no_images_found")
    _format_name, suffix = _format(output_format, Path(f"output{_FORMATS[output_format.casefold()][1]}"))
    plan: list[tuple[Path, Path]] = []
    targets: set[str] = set()
    for source in sources:
        target = destination / source.relative_to(source_dir).with_suffix(suffix)
        # Conservative case folding also protects case-insensitive filesystems.
        identity = str(target.resolve()).casefold()
        if identity in targets or target.exists() or target.is_symlink():
            raise AsxError(
                f"输出路径冲突，未转换任何图片：{target}。请更换输出目录或处理同名文件。",
                code="image_output_conflict",
            )
        targets.add(identity)
        plan.append((source, target))
    files: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for source, target in plan:
        try:
            image, _path, _source_format, _frames = _open_image(str(source))
            files.append(_save(image, str(target), output_format, exclusive=True))
        except AsxError as exc:
            failed.append({"path": str(source), "code": exc.code, "message": exc.message})
    return {"source_dir": str(source_dir), "output_dir": str(destination), "files": files, "failed": failed, "count": len(files), "failed_count": len(failed)}


def execute_local_image(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.image_command == "info":
        return {"ok": True, "image": _image_info(args.input)}, 0
    if args.image_command == "convert":
        return {"ok": True, "image": _save(_open_image(args.input)[0], args.output, args.format)}, 0
    if args.image_command == "resize":
        return {"ok": True, "image": _resize(args.input, args.output, args.width, args.height, args.format)}, 0
    if args.image_command == "crop":
        return {"ok": True, "image": _crop(args.input, args.output, args.box, args.format)}, 0
    if args.image_command == "slice":
        return {"ok": True, "slice": _slice(args.input, args.output_dir, args.rows, args.columns, args.prefix, args.format)}, 0
    if args.image_command == "contact-sheet":
        return {"ok": True, "image": _contact_sheet(args.inputs, args.output, args.columns, args.cell_width, args.cell_height, args.background, args.format)}, 0
    if args.image_command == "apply-mask":
        return {"ok": True, "image": _apply_mask(args.input, args.mask, args.output, args.format)}, 0
    if args.image_command == "batch-convert":
        payload = _batch_convert(args.input_dir, args.output_dir, args.format, args.recursive)
        return {"ok": payload["failed_count"] == 0, "batch": payload}, 0 if payload["failed_count"] == 0 else 2
    raise AssertionError("unreachable")
