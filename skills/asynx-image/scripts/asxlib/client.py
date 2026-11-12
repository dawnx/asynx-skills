from __future__ import annotations

import hashlib
import ipaddress
import json
import random
import time
from http.client import IncompleteRead
from pathlib import Path
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
from .errors import AsxError, service_error_message


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
        raise AsxError("服务返回的内容无法识别，请稍后查询原任务；若持续出现，请联系服务支持。", code="invalid_api_response") from exc
    if not isinstance(payload, dict):
        raise AsxError("服务返回的内容格式不正确，请稍后查询原任务；若持续出现，请联系服务支持。", code="invalid_api_response")
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
                "服务返回的内容过大，无法接收。请保留任务 ID 并联系服务支持。",
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
                "服务返回的内容过大，无法接收。请保留任务 ID 并联系服务支持。",
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
        request_id = payload.get("request_id") or request_id
        data = payload.get("data")
        task_id = data.get("task_id") if isinstance(data, dict) else None
        details = data.get("details") if isinstance(data, dict) else None
        message = service_error_message(code, http_status=status, message=payload.get("message"), details=details)
    else:
        code = f"http_{status}"
        message = service_error_message(code, http_status=status)
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
    message = "结果文件的跳转地址无效，请查询原任务后重新下载。" if redirect else "结果文件的下载地址无效，请查询原任务后重新下载。"
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
        raise AsxError("结果下载地址指向本地或内网，已停止下载。请检查服务配置，或联系服务支持。", code=code)
    return value


def _validate_api_redirect(value: str, *, base_url: str) -> str:
    """Validate and constrain API redirects to the configured origin."""
    message = "服务返回的跳转地址无效，请检查服务地址，或联系服务支持。"
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
            "服务要求跳转到其他站点，为保护密钥已停止请求。请检查服务地址，或联系服务支持。",
            code="invalid_api_redirect",
        )
    return value


class AsynxClient:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = validate_base_url(base_url)
        self.api_key = api_key

    @property
    def cache_scope(self) -> str:
        return hashlib.sha256(self.api_key.encode("utf-8")).hexdigest()

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
        unanswered = False
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
                                "服务未提供有效的跳转地址，请检查服务地址，或联系服务支持。",
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
                    error = _api_error(exc.code, raw, exc.headers)
                    if (
                        retry_safe
                        and error.retry_in_place
                        and exc.code in TRANSIENT_HTTP_STATUSES
                        and attempt + 1 < MAX_HTTP_ATTEMPTS
                    ):
                        unanswered = unanswered or error.submission_outcome is None
                        time.sleep(_retry_after(exc.headers, attempt))
                        continue
                    error.replayed = unanswered
                    raise error from exc
                except (URLError, TimeoutError, OSError) as exc:
                    if retry_safe and attempt + 1 < MAX_HTTP_ATTEMPTS:
                        unanswered = True
                        time.sleep(_retry_after(None, attempt))
                        continue
                    raise AsxError(
                        "暂时无法连接 Asynx，请检查网络和服务地址。若任务已提交，请先查询原任务，避免重复生成。",
                        code="network_error",
                        exit_code=3,
                    ) from exc

                payload = _decode_json(raw, context="Asynx API")
                request_id = payload.get("request_id") or response_headers.get("X-Request-ID")
                if payload.get("code") != "ok":
                    raise _api_error(status, raw, response_headers)
                data = payload.get("data")
                if not isinstance(data, dict):
                    raise AsxError("服务返回的内容不完整，请稍后查询原任务；若持续出现，请联系服务支持。", code="invalid_api_response")
                return data, request_id if isinstance(request_id, str) else None
            if redirected:
                continue
            raise AssertionError("unreachable")
        raise AsxError("服务反复重定向，已停止请求。请检查服务地址，或联系服务支持。", code="invalid_api_redirect")

    def models(self) -> tuple[list[dict[str, Any]], str | None]:
        data, request_id = self.request_json("GET", "/v1/tasks/models", retry_safe=True)
        items = data.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise AsxError("暂时无法获取可用模型列表，请稍后重试；若持续出现，请联系服务支持。", code="invalid_model_catalog")
        media_models = [item for item in items if item.get("modality") in {"image", "video", "model"}]
        return media_models, request_id

    def model_guide(self, model: str, *, examples: bool = False) -> tuple[dict[str, Any], str | None]:
        if not model.strip():
            raise AsxError("请指定要查询说明的模型名称。", code="invalid_model")
        path = f"/v1/tasks/models/{quote(model, safe='')}/guide"
        if examples:
            path += "?examples=true"
        data, request_id = self.request_json("GET", path, retry_safe=True)
        fields = ("model", "revision", "status", "content")
        if (
            not all(isinstance(data.get(field), str) for field in fields)
            or data["status"] not in {"documented", "unconfirmed"}
        ):
            raise AsxError("模型说明格式不完整，请稍后重试或联系服务支持。", code="invalid_model_guide")
        return {field: data[field] for field in fields}, request_id

    def submit(
        self, body: dict[str, Any], idempotency_key: str
    ) -> tuple[dict[str, Any], str | None]:
        try:
            return self.request_json(
                "POST",
                "/v1/tasks",
                body=body,
                idempotency_key=idempotency_key,
                retry_safe=True,
            )
        except AsxError as exc:
            # Only this request creates tasks, so only its errors report whether one exists.
            exc.for_submission = True
            raise

    def quote(self, body: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """Price a request with the service's own planner; creates and charges nothing."""
        return self.request_json("POST", "/v1/tasks/quote", body=body, retry_safe=True)

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
                                "结果文件缺少有效的下载地址，请查询原任务后重新下载。",
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
                        "结果文件暂时无法下载，请检查网络后继续下载原任务，无需重新生成。",
                        code="asset_download_failed",
                        exit_code=3,
                        task_id=task_id,
                    ) from exc
            else:
                raise AssertionError("unreachable")
            continue
        raise AsxError("结果下载地址反复跳转，已停止下载。请查询原任务后重新下载，或联系服务支持。", code="asset_redirect_limit")

    def asset_to_file(
        self,
        task_id: str,
        index: int,
        destination: Path,
        download_url: str | None = None,
        *,
        metrics: dict[str, Any] | None = None,
        max_bytes: int = 512 * 1024 * 1024,
        model_content_type: str | None = None,
    ) -> str | None:
        """Stream into a caller-owned temporary file; publication is the caller's job.

        ``model_content_type`` is the type the service declared for a 3D result,
        which most 3D formats need to be recognized at all.
        """
        from .media_results import media_file_info

        if max_bytes <= 0:
            raise AsxError("下载大小上限必须为正数。", code="invalid_download_limit")
        allow_local = urlsplit(self.base_url).hostname in {"localhost", "127.0.0.1", "::1"}
        url = (
            self._url(f"/v1/tasks/{quote(task_id, safe='')}/assets/{index}")
            if download_url is None
            else _validate_asset_url(download_url, allow_local=allow_local)
        )
        if metrics is not None:
            metrics.update(source="download_url" if download_url else "asset_endpoint", download_url=download_url)
        opener = build_opener(_NoRedirect())
        complete = False
        try:
            for _redirect in range(MAX_REDIRECTS + 1):
                headers = {"User-Agent": f"asx-skill/{VERSION}"}
                if _origin(url) == _origin(self.base_url):
                    headers["Authorization"] = f"Bearer {self.api_key}"
                for attempt in range(MAX_HTTP_ATTEMPTS):
                    request_started = time.perf_counter()
                    try:
                        with opener.open(Request(url, headers=headers, method="GET"), timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                            response_started = time.perf_counter()
                            declared: int | None = None
                            raw_length = response.headers.get("Content-Length")
                            if raw_length is not None:
                                try:
                                    declared = int(raw_length)
                                except ValueError as exc:
                                    raise AsxError("下载服务返回的文件大小无效，请查询原任务后重新下载。", code="invalid_asset_response") from exc
                                if declared < 0:
                                    raise AsxError("下载服务返回的文件大小无效，请查询原任务后重新下载。", code="invalid_asset_response")
                                if declared > max_bytes:
                                    raise AsxError("结果文件超过本地下载大小上限，请联系服务支持获取文件。", code="response_too_large")
                            total = 0
                            # A retry starts from byte zero, never appends a partial response.
                            with destination.open("wb") as target:
                                while True:
                                    chunk = response.read(min(64 * 1024, max_bytes + 1 - total))
                                    if not chunk:
                                        break
                                    total += len(chunk)
                                    if total > max_bytes:
                                        raise AsxError("结果文件超过本地下载大小上限，请联系服务支持获取文件。", code="response_too_large")
                                    target.write(chunk)
                            if declared is not None and total != declared:
                                raise OSError("下载响应提前结束，文件未完整传输。")
                            media_file_info(destination, model_content_type_hint=model_content_type)
                            if metrics is not None:
                                metrics.update(
                                    ttfb_seconds=response_started - request_started,
                                    transfer_seconds=time.perf_counter() - response_started,
                                    bytes=total,
                                )
                            complete = True
                            content_type = response.headers.get("Content-Type")
                            return content_type if isinstance(content_type, str) else None
                    except HTTPError as exc:
                        if exc.code in {301, 302, 303, 307, 308}:
                            location = exc.headers.get("Location")
                            exc.close()
                            if not location:
                                raise AsxError("结果文件缺少有效的下载地址，请查询原任务后重新下载。", code="invalid_asset_redirect") from exc
                            url = _validate_asset_url(urljoin(url, location), redirect=True, allow_local=allow_local)
                            break
                        try:
                            raw = _read_limited(exc, limit=MAX_API_RESPONSE_BYTES, context="Asset error")
                        finally:
                            exc.close()
                        if exc.code in TRANSIENT_HTTP_STATUSES and attempt + 1 < MAX_HTTP_ATTEMPTS:
                            time.sleep(_retry_after(exc.headers, attempt))
                            continue
                        raise _api_error(exc.code, raw, exc.headers) from exc
                    except (URLError, TimeoutError, OSError, IncompleteRead) as exc:
                        if attempt + 1 < MAX_HTTP_ATTEMPTS:
                            time.sleep(_retry_after(None, attempt))
                            continue
                        raise AsxError(
                            "结果文件下载未完成，请检查网络后继续下载原任务，无需重新生成。", code="asset_download_failed", exit_code=3, task_id=task_id,
                        ) from exc
                else:
                    raise AssertionError("unreachable")
            raise AsxError("结果下载地址反复跳转，已停止下载。请查询原任务后重新下载，或联系服务支持。", code="asset_redirect_limit")
        finally:
            if not complete:
                destination.unlink(missing_ok=True)
