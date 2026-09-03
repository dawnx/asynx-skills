from __future__ import annotations

import json
import random
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .config import validate_base_url
from .constants import (
    DOWNLOAD_TIMEOUT_SECONDS,
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

        for attempt in range(MAX_HTTP_ATTEMPTS):
            request = Request(self._url(path), data=encoded, headers=headers, method=method)
            try:
                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    raw = response.read()
                    response_headers = response.headers
                    status = response.status
            except HTTPError as exc:
                raw = exc.read()
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
        raise AssertionError("unreachable")

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

    def asset(self, task_id: str, index: int) -> tuple[bytes, str | None]:
        url = self._url(f"/v1/tasks/{quote(task_id, safe='')}/assets/{index}")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": f"asx-skill/{VERSION}",
        }
        opener = build_opener(_NoRedirect())
        for _redirect in range(MAX_REDIRECTS + 1):
            for attempt in range(MAX_HTTP_ATTEMPTS):
                request = Request(url, headers=headers, method="GET")
                try:
                    with opener.open(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                        return response.read(), response.headers.get("Content-Type")
                except HTTPError as exc:
                    if exc.code in {301, 302, 303, 307, 308}:
                        location = exc.headers.get("Location")
                        if not location:
                            raise AsxError(
                                "Asset redirect is missing Location",
                                code="invalid_asset_redirect",
                            ) from exc
                        next_url = urljoin(url, location)
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
