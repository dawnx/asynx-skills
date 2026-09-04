from __future__ import annotations

import ipaddress
import json
import random
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import validate_base_url
from .constants import (
    DOWNLOAD_TIMEOUT_SECONDS,
    MAX_API_RESPONSE_BYTES,
    MAX_ASSET_RESPONSE_BYTES,
    MAX_HTTP_ATTEMPTS,
    MAX_REDIRECTS,
    REQUEST_TIMEOUT_SECONDS,
    TRANSIENT_HTTP_STATUSES,
    VERSION,
)
from .errors import AsxError


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _decode_json(raw: bytes, *, context: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AsxError(f"{context} returned invalid JSON", code="invalid_api_response") from exc
    if not isinstance(payload, dict):
        raise AsxError(f"{context} returned a non-object JSON response", code="invalid_api_response")
    return payload


def _read_limited(response: Any, *, limit: int, context: str) -> bytes:
    """Read a response without allowing an untrusted peer to exhaust memory."""
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except (TypeError, ValueError):
            declared_length = None
        if declared_length is not None and declared_length > limit:
            raise AsxError(
                f"{context} response exceeds the {limit} byte limit",
                code="response_too_large",
            )

    chunks = bytearray()
    while len(chunks) <= limit:
        chunk = response.read(min(64 * 1024, limit + 1 - len(chunks)))
        if not chunk:
            return bytes(chunks)
        chunks.extend(chunk)
        if len(chunks) > limit:
            raise AsxError(
                f"{context} response exceeds the {limit} byte limit",
                code="response_too_large",
            )
    raise AssertionError("unreachable")


def _api_error(status: int, raw: bytes, headers: Any) -> AsxError:
    request_id = headers.get("X-Request-ID") if headers is not None else None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = {}
    if isinstance(payload, dict):
        code = str(payload.get("code") or f"http_{status}")
        message = str(payload.get("message") or f"Asynx API returned HTTP {status}")
        request_id = payload.get("request_id") or request_id
        data = payload.get("data")
        task_id = data.get("task_id") if isinstance(data, dict) else None
        details = data.get("details") if isinstance(data, dict) else None
    else:
        code = f"http_{status}"
        message = f"Asynx API returned HTTP {status}"
        task_id = None
        details = None
    return AsxError(
        message,
        code=code,
        exit_code=3,
        http_status=status,
        request_id=request_id if isinstance(request_id, str) else None,
        task_id=task_id if isinstance(task_id, str) else None,
        details=details,
    )


def _retry_after(headers: Any, attempt: int) -> float:
    value = headers.get("Retry-After") if headers is not None else None
    if value:
        try:
            return min(max(float(value), 0.0), 60.0)
        except ValueError:
            pass
    base = min(2**attempt, 8)
    return float(base) + random.uniform(0.0, float(base) * 0.2)


def _origin(value: str) -> tuple[str, str, int | None]:
    parts = urlsplit(value)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def _validate_asset_url(
    value: str, *, redirect: bool = False, allow_local: bool = False
) -> str:
    code = "invalid_asset_redirect" if redirect else "invalid_asset_url"
    message = "Asset redirect URL is invalid" if redirect else "Asset download URL is invalid"
    if value != value.strip() or any(char.isspace() for char in value):
        raise AsxError(message, code=code)
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise AsxError(message, code=code) from exc
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise AsxError(message, code=code)
    try:
        address = ipaddress.ip_address(parts.hostname)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private or address.is_loopback or address.is_link_local
    ) and not allow_local:
        raise AsxError("Asset download URL points to a private network", code=code)
    return value


def _validate_api_redirect(value: str, *, base_url: str) -> str:
    """Validate and constrain API redirects to the configured origin."""
    message = "Asynx API redirect is invalid"
    if value != value.strip() or any(char.isspace() for char in value):
        raise AsxError(message, code="invalid_api_redirect")
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise AsxError(message, code="invalid_api_redirect") from exc
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise AsxError(message, code="invalid_api_redirect")
    if _origin(value) != _origin(base_url):
        raise AsxError(
            "Asynx API redirects must remain on the configured origin",
            code="invalid_api_redirect",
        )
    return value


class AsynxClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = validate_base_url(base_url)
        self.api_key = api_key

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def request_json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        retry_safe: bool = False,
    ) -> tuple[dict[str, Any], str | None]:
        encoded = (
            json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if body is not None
            else None
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": f"asx-skill/{VERSION}",
        }
        if encoded is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        url = self._url(path)
        opener = build_opener(_NoRedirect())
        for _redirect in range(MAX_REDIRECTS + 1):
            redirected = False
            for attempt in range(MAX_HTTP_ATTEMPTS):
                request = Request(url, data=encoded, headers=headers, method=method)
                try:
                    with opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                        raw = _read_limited(
                            response, limit=MAX_API_RESPONSE_BYTES, context="Asynx API"
                        )
                        response_headers = response.headers
                        status = response.status
                except HTTPError as exc:
                    if exc.code in {301, 302, 303, 307, 308}:
                        location = exc.headers.get("Location")
                        if not location:
                            raise AsxError(
                                "Asynx API redirect is missing Location",
                                code="invalid_api_redirect",
                            ) from exc
                        url = _validate_api_redirect(
                            urljoin(url, location), base_url=self.base_url
                        )
                        redirected = True
                        break
                    raw = _read_limited(
                        exc, limit=MAX_API_RESPONSE_BYTES, context="Asynx API error"
                    )
                    if (
                        retry_safe
                        and exc.code in TRANSIENT_HTTP_STATUSES
                        and attempt + 1 < MAX_HTTP_ATTEMPTS
                    ):
                        time.sleep(_retry_after(exc.headers, attempt))
                        continue
                    raise _api_error(exc.code, raw, exc.headers) from exc
                except (URLError, TimeoutError, OSError) as exc:
                    if retry_safe and attempt + 1 < MAX_HTTP_ATTEMPTS:
                        time.sleep(_retry_after(None, attempt))
                        continue
                    raise AsxError(
                        f"Cannot reach the Asynx API: {exc}",
                        code="network_error",
                        exit_code=3,
                    ) from exc

                payload = _decode_json(raw, context="Asynx API")
                request_id = payload.get("request_id") or response_headers.get("X-Request-ID")
                if payload.get("code") != "ok":
                    raise _api_error(status, raw, response_headers)
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise AsxError("Asynx API response is missing data", code="invalid_api_response")
                return data, request_id if isinstance(request_id, str) else None
            if redirected:
                continue
            raise AssertionError("unreachable")
        raise AsxError("Asynx API redirect limit exceeded", code="invalid_api_redirect")

    def models(self) -> tuple[list[dict[str, Any]], str | None]:
        data, request_id = self.request_json("GET", "/v1/tasks/models", retry_safe=True)
        items = data.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise AsxError("Model catalog is invalid", code="invalid_model_catalog")
        image_models = [item for item in items if item.get("modality") == "image"]
        return image_models, request_id

    def submit(
        self, body: dict[str, Any], idempotency_key: str
    ) -> tuple[dict[str, Any], str | None]:
        return self.request_json(
            "POST",
            "/v1/tasks",
            body=body,
            idempotency_key=idempotency_key,
            retry_safe=True,
        )

    def task(self, task_id: str) -> tuple[dict[str, Any], str | None]:
        return self.request_json("GET", f"/v1/tasks/{quote(task_id, safe='')}", retry_safe=True)

    def cancel(self, task_id: str) -> tuple[dict[str, Any], str | None]:
        return self.request_json("POST", f"/v1/tasks/{quote(task_id, safe='')}/cancel")

    def list_tasks(
        self,
        *,
        status: str | None = None,
        model: str | None = None,
        limit: int = 20,
    ) -> tuple[dict[str, Any], str | None]:
        query: list[str] = [f"limit={max(1, min(limit, 100))}"]
        if status:
            query.append(f"status={quote(status, safe='')}")
        if model:
            query.append(f"model={quote(model, safe='')}")
        return self.request_json("GET", "/v1/tasks?" + "&".join(query), retry_safe=True)

    def asset(
        self,
        task_id: str,
        index: int,
        download_url: str | None = None,
        *,
        metrics: dict[str, Any] | None = None,
    ) -> tuple[bytes, str | None]:
        url = (
            self._url(f"/v1/tasks/{quote(task_id, safe='')}/assets/{index}")
            if download_url is None
            else _validate_asset_url(
                download_url,
                allow_local=urlsplit(self.base_url).hostname in {"localhost", "127.0.0.1", "::1"},
            )
        )
        if metrics is not None:
            metrics.update(
                {
                    "source": "download_url" if download_url is not None else "asset_endpoint",
                    "download_url": download_url,
                }
            )
        headers = {"User-Agent": f"asx-skill/{VERSION}"}
        if _origin(url) == _origin(self.base_url):
            headers["Authorization"] = f"Bearer {self.api_key}"
        opener = build_opener(_NoRedirect())
        for _redirect in range(MAX_REDIRECTS + 1):
            for attempt in range(MAX_HTTP_ATTEMPTS):
                request = Request(url, headers=headers, method="GET")
                request_started = time.perf_counter()
                try:
                    with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                        response_started = time.perf_counter()
                        raw = _read_limited(
                            response, limit=MAX_ASSET_RESPONSE_BYTES, context="Asset"
                        )
                        if metrics is not None:
                            metrics.update(
                                {
                                    "ttfb_seconds": response_started - request_started,
                                    "transfer_seconds": time.perf_counter() - response_started,
                                    "bytes": len(raw),
                                }
                            )
                        return raw, response.headers.get("Content-Type")
                except HTTPError as exc:
                    if exc.code in {301, 302, 303, 307, 308}:
                        location = exc.headers.get("Location")
                        if not location:
                            raise AsxError(
                                "Asset redirect is missing Location",
                                code="invalid_asset_redirect",
                            ) from exc
                        next_url = urljoin(url, location)
                        next_url = _validate_asset_url(
                            next_url,
                            redirect=True,
                            allow_local=urlsplit(self.base_url).hostname
                            in {"localhost", "127.0.0.1", "::1"},
                        )
                        headers = {"User-Agent": f"asx-skill/{VERSION}"}
                        if _origin(next_url) == _origin(self.base_url):
                            headers["Authorization"] = f"Bearer {self.api_key}"
                        url = next_url
                        break
                    raw = exc.read()
                    if (
                        exc.code in TRANSIENT_HTTP_STATUSES
                        and attempt + 1 < MAX_HTTP_ATTEMPTS
                    ):
                        time.sleep(_retry_after(exc.headers, attempt))
                        continue
                    raise _api_error(exc.code, raw, exc.headers) from exc
                except (URLError, TimeoutError, OSError) as exc:
                    if attempt + 1 < MAX_HTTP_ATTEMPTS:
                        time.sleep(_retry_after(None, attempt))
                        continue
                    raise AsxError(
                        f"Cannot download the Asynx Asset: {exc}",
                        code="asset_download_failed",
                        exit_code=3,
                        task_id=task_id,
                    ) from exc
            else:
                raise AssertionError("unreachable")
            continue
        raise AsxError("Asset download exceeded the redirect limit", code="asset_redirect_limit")
