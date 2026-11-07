"""Validate static SVG delivery without rasterizing the original asset."""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from fractions import Fraction
from typing import Any

from .constants import MAX_ASSET_RESPONSE_BYTES
from .errors import AsxError

_SVG_ELEMENTS = {
    "svg", "g", "defs", "path", "rect", "circle", "ellipse", "line", "polyline",
    "polygon", "text", "tspan", "textPath", "title", "desc", "linearGradient",
    "radialGradient", "stop", "clipPath", "mask", "pattern", "use", "symbol",
    "style", "marker", "filter", "feGaussianBlur", "feOffset", "feBlend",
    "feColorMatrix", "feComposite", "feFlood", "feMerge", "feMergeNode",
    "feComponentTransfer", "feFuncR", "feFuncG", "feFuncB", "feFuncA", "feDropShadow",
}
_FRAGMENT = re.compile(r"#[A-Za-z_][\w:.-]*")


def _validate_css(value: str) -> None:
    if any(token in value for token in ("\\", "/*", "*/", "@")) or re.search(
        r"(?:javascript|data|https?|file):|expression\s*\(|-moz-binding|behavior\s*:", value, re.IGNORECASE
    ):
        raise ValueError("active CSS")
    starts = list(re.finditer(r"url\s*\(", value, re.IGNORECASE))
    urls = list(re.finditer(r"url\s*\((.*?)\)", value, re.IGNORECASE | re.DOTALL))
    if len(starts) != len(urls):
        raise ValueError("unclosed CSS URL")
    for match in urls:
        target = match.group(1).strip()
        if target.startswith(("'", '"')):
            if len(target) < 2 or target[-1] != target[0]:
                raise ValueError("unclosed CSS quote")
            target = target[1:-1].strip()
        if not _FRAGMENT.fullmatch(target):
            raise ValueError("external CSS URL")


def _ratio(width: float, height: float) -> str:
    ratio = Fraction(str(width)) / Fraction(str(height))
    return f"{ratio.numerator}:{ratio.denominator}"


def is_svg(data: bytes) -> bool:
    # Inspect the root opening tag, after XML declarations/comments. HTML error
    # responses must remain unrecognized media, even if they contain SVG icons.
    prefix = data[:65536].lstrip(b"\xef\xbb\xbf \t\r\n")
    prefix = re.sub(br"^(?:(?:<\?.*?\?>|<!--.*?-->)\s*)+", b"", prefix, flags=re.DOTALL)
    return re.match(br"<(?:[A-Za-z_][\w.-]*:)?svg(?:\s|/?>)", prefix) is not None


def svg_details(data: bytes) -> dict[str, Any]:
    try:
        if len(data) > MAX_ASSET_RESPONSE_BYTES:
            raise ValueError("SVG byte limit")
        text = data.decode("utf-8-sig")
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)|<\?(?!xml\s)", text, re.IGNORECASE):
            raise ValueError("XML entities")
        root = ET.fromstring(text)
        if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
            raise ValueError("not SVG")
        for index, element in enumerate(root.iter()):
            tag = element.tag.removeprefix("{http://www.w3.org/2000/svg}")
            if index >= 100_000 or tag not in _SVG_ELEMENTS:
                raise ValueError("active SVG")
            for key, value in element.attrib.items():
                key = key.rsplit("}", 1)[-1].lower()
                if key.startswith("on") or key in {"base", "src"} or (key == "href" and not _FRAGMENT.fullmatch(value)):
                    raise ValueError("external or executable SVG")
                _validate_css(value)
            if tag == "style":
                _validate_css("".join(element.itertext()))
        details: dict[str, Any] = {"format": "svg"}
        for key in ("width", "height"):
            raw = root.get(key, "")
            if re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)(?:px)?", raw):
                number = float(raw.removesuffix("px"))
                if not math.isfinite(number) or number <= 0:
                    raise ValueError("invalid SVG dimension")
                details[key] = int(number) if number.is_integer() else number
        if "width" in details and "height" in details:
            details["aspect_ratio"] = _ratio(details["width"], details["height"])
        if root.get("viewBox"):
            box = [float(value) for value in re.split(r"[\s,]+", root.attrib["viewBox"].strip())]
            if len(box) != 4 or not all(math.isfinite(value) for value in box) or min(box[2:]) <= 0:
                raise ValueError("invalid SVG viewBox")
            # These are user-space coordinates, not measured output pixels.
            details["view_box"] = box
            details["view_box_aspect_ratio"] = _ratio(box[2], box[3])
        return details
    except (ET.ParseError, ValueError) as exc:
        raise AsxError("SVG 结果无效或包含不支持的活动内容，未保存。", code="invalid_svg_result") from exc
