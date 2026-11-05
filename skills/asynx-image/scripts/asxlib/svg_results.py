"""Validate static SVG delivery without rasterizing the original asset."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from .errors import AsxError


def is_svg(data: bytes) -> bool:
    # Inspect the root opening tag, after XML declarations/comments. HTML error
    # responses must remain unrecognized media, even if they contain SVG icons.
    prefix = data[:65536].lstrip(b"\xef\xbb\xbf \t\r\n")
    prefix = re.sub(br"^(?:(?:<\?.*?\?>|<!--.*?-->)\s*)+", b"", prefix, flags=re.S)
    return re.match(br"<(?:[A-Za-z_][\w.-]*:)?svg(?:\s|/?>)", prefix) is not None


def svg_details(data: bytes) -> dict[str, Any]:
    try:
        if re.search(br"<!\s*(?:DOCTYPE|ENTITY)|<\?(?!xml\s)", data, re.I):
            raise ValueError("XML entities")
        root = ET.fromstring(data)
        if root.tag not in {"svg", "{http://www.w3.org/2000/svg}svg"}:
            raise ValueError("not SVG")
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1].lower() in {"script", "foreignobject", "iframe", "object", "embed", "animate", "set", "animatetransform", "animatemotion"}:
                raise ValueError("active SVG")
            for key, value in element.attrib.items():
                key = key.rsplit("}", 1)[-1].lower()
                if key.startswith("on") or (key in {"href", "src"} and not value.startswith("#")):
                    raise ValueError("external or executable SVG")
            css = " ".join(element.attrib.values()) + (element.text or "")
            if re.search(r"@import|url\(\s*['\"]?\s*(?!#)", css, re.I):
                raise ValueError("external CSS")
        details: dict[str, Any] = {"format": "svg"}
        for key in ("width", "height"):
            raw = root.get(key, "")
            if re.fullmatch(r"[1-9]\d*(?:px)?", raw):
                details[key] = int(raw.removesuffix("px"))
        return details
    except (ET.ParseError, ValueError) as exc:
        raise AsxError("SVG 结果无效或包含不支持的活动内容，未保存。", code="invalid_svg_result") from exc
