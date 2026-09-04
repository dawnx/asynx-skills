from __future__ import annotations

import argparse
import hashlib
import os
import random
import re
import secrets
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from .client import AsynxClient
from .constants import (
    DEFAULT_OUTPUT_DIR,
    KNOWN_STATUSES,
    MAX_TASK_POLL_DELAY_SECONDS,
    TERMINAL_STATUSES,
    UTC,
)
from .errors import AsxError
from .images import (
    build_task,
    cache_models,
    image_mime,
    task_input_warnings,
    validate_idempotency_key,
)
from .output import log
from .state import (
    append_event,
    bind_remote_task,
    create_task_intent,
    list_assets,
    update_task,
    upsert_asset,
    utc_now,
)
from .state import (
    connect_db as connect_ledger_db,
)
from .state import (
    get_task as get_ledger_task,
)
from .state import (
    list_tasks as list_ledger_tasks,
)
from .state import (
    task_paths as ledger_task_paths,
)

_SAFE_PATH_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


class TaskStatusClient(Protocol):
    def task(self, task_id: str) -> tuple[dict[str, Any], str | None]: ...


def parse_deadline(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def wait_for_terminal(
    client: TaskStatusClient,
    task: dict[str, Any],
    request_id: str | None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[dict[str, Any], str | None]:
    task_id = task.get("id")
    if not isinstance(task_id, str):
        raise AsxError("Task response is missing an ID", code="invalid_task_response")
    deadline = parse_deadline(task.get("deadline_at"))
    client_deadline = (
        deadline + timedelta(seconds=30)
        if deadline
        else datetime.now(UTC) + timedelta(minutes=20)
    )
    delay = 2.0
    last_status: str | None = None
    while True:
        status = task.get("status")
        if not isinstance(status, str) or status not in KNOWN_STATUSES:
            raise AsxError(
                f"Task {task_id} 返回了未知状态",
                code="invalid_task_status",
                task_id=task_id,
            )
        if status != last_status:
            log(f"Task {task_id}：{status}")
            last_status = status
        if status in TERMINAL_STATUSES:
            return task, request_id
        if datetime.now(UTC) >= client_deadline:
            raise AsxError(
                f"Stopped waiting for Task {task_id} after its execution deadline",
                code="client_wait_timeout",
                exit_code=4,
                task_id=task_id,
                request_id=request_id,
            )
        sleep(delay + random.uniform(0.0, delay * 0.15))
        delay = min(delay * 1.5, MAX_TASK_POLL_DELAY_SECONDS)
        task, request_id = client.task(task_id)


def _extension(content_type: str | None, data: bytes) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return "jpg"
    if normalized == "image/webp":
        return "webp"
    if normalized == "image/png":
        return "png"
    return {"image/jpeg": "jpg", "image/webp": "webp", "image/png": "png"}[
        image_mime(data)
    ]


def _unique_output_path(directory: Path, base_name: str, extension: str) -> Path:
    safe_name = _SAFE_PATH_COMPONENT.sub("_", base_name).strip("._") or "asset"
    return directory / f"{safe_name}.{extension}"


def download_assets(
    client: AsynxClient,
    task: dict[str, Any],
    output_dir: str,
    *,
    prefix: str | None = None,
    metrics: list[dict[str, Any]] | None = None,
    local_task_id: str | None = None,
) -> list[str]:
    task_id = task.get("id")
    result = task.get("result")
    assets = result.get("assets") if isinstance(result, dict) else None
    if not isinstance(task_id, str) or not isinstance(assets, list) or not assets:
        raise AsxError(
            "Succeeded Task did not contain downloadable Assets",
            code="missing_task_assets",
            task_id=task_id if isinstance(task_id, str) else None,
        )
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    cached_assets: dict[int, dict[str, Any]] = {}
    if local_task_id:
        cache_connection = connect_ledger_db()
        try:
            cached_assets = {
                int(item["remote_index"]): item
                for item in list_assets(cache_connection, local_task_id)
            }
        finally:
            cache_connection.close()
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("index"), int):
            raise AsxError(
                "Task contains an invalid Asset",
                code="invalid_task_asset",
                task_id=task_id,
            )
        index = asset["index"]
        asset_metrics: dict[str, Any] = {}
        download_url = asset.get("download_url") if isinstance(asset.get("download_url"), str) else None
        cached = cached_assets.get(index)
        cached_path = cached.get("path") if cached else None
        if isinstance(cached_path, str) and cached_path and Path(cached_path).is_file():
            files.append(cached_path)
            asset_metrics.update(
                {
                    "source": "local_cache",
                    "bytes": Path(cached_path).stat().st_size,
                    "save_seconds": 0.0,
                }
            )
            if metrics is not None:
                metrics.append(asset_metrics)
            continue
        data, _response_type = client.asset(
            task_id, index, download_url, metrics=asset_metrics
        )
        actual_mime = image_mime(data)
        extension = _extension(actual_mime, data)
        path = (
            Path(cached_path)
            if isinstance(cached_path, str) and cached_path
            else _unique_output_path(directory, f"{prefix or task_id}-{index}", extension)
        )
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.part")
        save_started = time.perf_counter()
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise AsxError(f"Cannot save generated image: {exc}", code="output_write_failed") from exc
        asset_metrics["save_seconds"] = time.perf_counter() - save_started
        asset_metrics.pop("download_url", None)
        if metrics is not None:
            metrics.append(asset_metrics)
        if local_task_id:
            connection = connect_ledger_db()
            try:
                upsert_asset(
                    connection,
                    local_task_id,
                    index,
                    path=str(path),
                    remote_url=download_url,
                    mime=actual_mime,
                    bytes=len(data),
                    sha256=hashlib.sha256(data).hexdigest(),
                    state="available",
                )
                connection.commit()
            finally:
                connection.close()
        files.append(str(path))
    return files


def index_downloaded_artifacts(
    task: dict[str, Any], files: list[str], *, operation: str | None = None
) -> dict[str, Any] | None:
    remote_id = task.get("id")
    if not isinstance(remote_id, str):
        return {"code": "artifact_index_failed", "message": "结果缺少 Task ID，无法建立本地索引"}
    connection = connect_ledger_db()
    try:
        existing = get_ledger_task(connection, remote_task_id=remote_id)
        if existing is None:
            task_type = task.get("task_type")
            operation_value = operation or (
                task_type.removeprefix("image.") if isinstance(task_type, str) else "generate"
            )
            request: dict[str, Any] = {
                "task_type": task_type or f"image.{operation_value}",
                "model": task.get("model") or "unknown",
                "input": task.get("input") if isinstance(task.get("input"), dict) else {},
            }
            key = f"asx-recovered-{hashlib.sha256(remote_id.encode()).hexdigest()[:24]}"
            existing = create_task_intent(
                connection,
                operation=operation_value,
                model=str(task.get("model") or "unknown"),
                idempotency_key=key,
                request=request,
                output_dir=str(Path(files[0]).parent) if files else DEFAULT_OUTPUT_DIR,
                status="succeeded",
            )
            bind_remote_task(
                connection,
                str(existing["local_id"]),
                remote_id,
                remote=task,
                status="succeeded",
                last_polled=utc_now(),
            )
        local_id = str(existing["local_id"])
        for index, path in enumerate(files):
            data = Path(path).read_bytes()
            upsert_asset(
                connection,
                local_id,
                index,
                path=path,
                mime=image_mime(data),
                bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                state="available",
            )
        connection.commit()
    except (OSError, sqlite3.Error, AsxError) as exc:
        return {
            "code": "artifact_index_failed",
            "message": f"本地结果索引写入失败，图片仍已保存：{exc}",
        }
    finally:
        connection.close()
    return None


def _task_identifier_row(connection: sqlite3.Connection, identifier: str) -> dict[str, Any]:
    row = get_ledger_task(connection, local_task_id=identifier)
    if row is None:
        row = get_ledger_task(connection, remote_task_id=identifier)
    if row is None:
        raise AsxError(f"本地 Task 不存在：{identifier}", code="task_not_found")
    return row


def _local_task_payload(row: dict[str, Any]) -> dict[str, Any]:
    remote = row.get("remote")
    payload: dict[str, Any] = {
        "ok": row.get("status") not in {"failed", "timeout", "canceled", "orphaned"},
        "schema_version": 1,
        "local_id": row.get("local_id"),
        "task_id": row.get("remote_task_id"),
        "status": row.get("status"),
        "model": row.get("model"),
        "operation": row.get("operation"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_polled": row.get("last_polled"),
        "billing": row.get("billing"),
        "error": row.get("error"),
    }
    if isinstance(remote, dict):
        for key in ("result_quality", "result", "deadline_at", "started_at", "finished_at"):
            if remote.get(key) is not None:
                payload[key] = remote[key]
    status = str(row.get("status"))
    if status in {"intent", "submitting", "unknown"}:
        payload["next_action"] = "task recover"
    elif status in {"queued", "running", "delayed", "cancel_requested", "canceling"}:
        payload["next_action"] = "task poll"
    elif status == "succeeded":
        payload["next_action"] = "asset list"
    return {key: value for key, value in payload.items() if value is not None}


def _persist_task_intent(
    client: AsynxClient,
    body: dict[str, Any],
    *,
    operation: str,
    model: str,
    idempotency_key: str,
    output_dir: str,
) -> dict[str, Any]:
    local_id = f"local_{secrets.token_hex(12)}"
    resolved_output_dir = (
        str((Path(DEFAULT_OUTPUT_DIR) / local_id).resolve())
        if output_dir == DEFAULT_OUTPUT_DIR
        else str(Path(output_dir).expanduser().resolve())
    )
    connection = connect_ledger_db()
    try:
        row = create_task_intent(
            connection,
            operation=operation,
            model=model,
            idempotency_key=idempotency_key,
            request=body,
            output_dir=resolved_output_dir,
            base_url=client.base_url,
            local_id=local_id,
            status="submitting",
        )
        append_event(connection, str(row["local_id"]), "submit_started")
        connection.commit()
        return row
    finally:
        connection.close()


def _bind_submission(
    client: AsynxClient,
    local_id: str,
    task: dict[str, Any],
    request_id: str | None,
) -> dict[str, Any]:
    remote_id = task.get("id")
    if not isinstance(remote_id, str):
        raise AsxError("Task response is missing an ID", code="invalid_task_response")
    status = task.get("status") if isinstance(task.get("status"), str) else "queued"
    connection = connect_ledger_db()
    try:
        row = bind_remote_task(
            connection,
            local_id,
            remote_id,
            remote=task,
            status=status,
            last_polled=utc_now(),
        )
        update_task(
            connection,
            local_id,
            billing=task.get("billing") if task.get("billing") is not None else None,
        )
        append_event(
            connection,
            local_id,
            "submitted",
            {"request_id": request_id, "status": status},
        )
        connection.commit()
        return row
    finally:
        connection.close()


def _record_submit_error(local_id: str, exc: AsxError) -> None:
    connection = connect_ledger_db()
    try:
        update_task(connection, local_id, error=exc.payload().get("error"))
        append_event(connection, local_id, "submit_error", exc.payload().get("error"))
        connection.commit()
    finally:
        connection.close()


def _remote_task_for_row(client: AsynxClient, row: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    remote_id = row.get("remote_task_id")
    if not isinstance(remote_id, str):
        raise AsxError(
            "本地 Task 尚未绑定远端 Task，请执行 task recover",
            code="task_not_submitted",
            task_id=str(row.get("local_id")),
        )
    return client.task(remote_id)


def sync_local_task(
    client: AsynxClient,
    identifier: str,
    *,
    download: bool = True,
    output_dir: str | None = None,
) -> tuple[dict[str, Any], int]:
    connection = connect_ledger_db()
    try:
        row = _task_identifier_row(connection, identifier)
    finally:
        connection.close()
    task, request_id = _remote_task_for_row(client, row)
    status = task.get("status")
    if not isinstance(status, str) or status not in KNOWN_STATUSES:
        raise AsxError("Asynx returned an unknown task status", code="invalid_task_status")
    local_id = str(row["local_id"])
    previous_status = str(row.get("status"))
    local_status = (
        "canceling"
        if previous_status in {"cancel_requested", "canceling"}
        and status not in TERMINAL_STATUSES
        else status
    )
    connection = connect_ledger_db()
    try:
        update_task(
            connection,
            local_id,
            status=local_status,
            remote=task,
            error=task.get("error") if status in {"failed", "timeout", "canceled"} else None,
            billing=task.get("billing"),
            last_polled=utc_now(),
        )
        if local_status != previous_status:
            append_event(
                connection,
                local_id,
                "status_changed",
                {"from": previous_status, "to": local_status, "request_id": request_id},
            )
        connection.commit()
    finally:
        connection.close()
    files: list[str] = []
    if status == "succeeded" and download:
        target_dir = output_dir or str(row.get("output_dir") or DEFAULT_OUTPUT_DIR)
        files = download_assets(client, task, target_dir, local_task_id=local_id)
    connection = connect_ledger_db()
    try:
        current_row = get_ledger_task(connection, local_task_id=local_id) or row
    finally:
        connection.close()
    payload = _local_task_payload(current_row)
    if files:
        payload["files"] = files
    payload["request_id"] = request_id
    return payload, 0 if status not in {"failed", "timeout", "canceled"} else 4


def recover_tasks(client: AsynxClient, *, limit: int = 50) -> dict[str, Any]:
    connection = connect_ledger_db()
    try:
        rows = list_ledger_tasks(connection, limit=limit)
    finally:
        connection.close()
    results: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status"))
        if status in TERMINAL_STATUSES or status == "succeeded":
            continue
        try:
            if not row.get("remote_task_id"):
                body = row.get("request")
                if not isinstance(body, dict):
                    raise AsxError("本地请求体损坏，无法恢复", code="invalid_task_state")
                task, request_id = client.submit(body, str(row["idempotency_key"]))
                row = _bind_submission(client, str(row["local_id"]), task, request_id)
            synced, _code = sync_local_task(client, str(row["local_id"]))
            results.append(synced)
        except AsxError as exc:
            results.append({"ok": False, "local_id": row.get("local_id"), "error": exc.payload()["error"]})
    return {"ok": all(item.get("ok", False) for item in results), "tasks": results}


def local_tasks_payload(*, status: str | None = None, limit: int = 50) -> dict[str, Any]:
    connection = connect_ledger_db()
    try:
        rows = list_ledger_tasks(connection, status=status, limit=limit)
        return {"ok": True, "schema_version": 1, "tasks": [_local_task_payload(row) for row in rows]}
    finally:
        connection.close()


def local_task_status(identifier: str) -> dict[str, Any]:
    connection = connect_ledger_db()
    try:
        return {"ok": True, "schema_version": 1, "task": _local_task_payload(_task_identifier_row(connection, identifier))}
    finally:
        connection.close()


def local_asset_payload(identifier: str) -> dict[str, Any]:
    connection = connect_ledger_db()
    try:
        row = _task_identifier_row(connection, identifier)
        return {
            "ok": True,
            "schema_version": 1,
            "local_id": row["local_id"],
            "task_id": row.get("remote_task_id"),
            "assets": [
                {
                    **asset,
                    "missing": bool(asset.get("path"))
                    and not Path(str(asset["path"])).is_file(),
                }
                for asset in list_assets(connection, str(row["local_id"]))
            ],
        }
    finally:
        connection.close()


def poll_local_tasks(
    client: AsynxClient, identifier: str | None = None, *, limit: int = 50
) -> dict[str, Any]:
    connection = connect_ledger_db()
    try:
        if identifier:
            rows = [_task_identifier_row(connection, identifier)]
        else:
            rows = list_ledger_tasks(connection, limit=limit)
    finally:
        connection.close()
    results: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status"))
        if status in TERMINAL_STATUSES and status != "succeeded":
            results.append(_local_task_payload(row))
            continue
        if not row.get("remote_task_id"):
            results.append(_local_task_payload(row))
            continue
        try:
            synced, _code = sync_local_task(client, str(row["local_id"]))
            results.append(synced)
        except AsxError as exc:
            results.append(
                {
                    "ok": False,
                    "local_id": row.get("local_id"),
                    "task_id": row.get("remote_task_id"),
                    "error": exc.payload().get("error"),
                }
            )
    return {"ok": all(item.get("ok", False) for item in results), "schema_version": 1, "tasks": results}


def cancel_local_task(client: AsynxClient, identifier: str) -> tuple[dict[str, Any], int]:
    connection = connect_ledger_db()
    try:
        row = _task_identifier_row(connection, identifier)
    finally:
        connection.close()
    local_id = str(row["local_id"])
    remote_id = row.get("remote_task_id")
    if not isinstance(remote_id, str):
        connection = connect_ledger_db()
        try:
            update_task(connection, local_id, status="canceled")
            append_event(connection, local_id, "canceled", {"remote": False})
            connection.commit()
        finally:
            connection.close()
        return local_task_status(local_id), 0
    task, request_id = client.cancel(remote_id)
    remote_status = task.get("status")
    status = remote_status if isinstance(remote_status, str) else "canceling"
    local_status = status if status in TERMINAL_STATUSES else "cancel_requested"
    connection = connect_ledger_db()
    try:
        update_task(
            connection,
            local_id,
            status=local_status,
            remote=task,
            billing=task.get("billing"),
            error=task.get("error") if local_status in TERMINAL_STATUSES else None,
            last_polled=utc_now(),
        )
        append_event(
            connection,
            local_id,
            "cancel_requested",
            {"request_id": request_id, "remote_status": status},
        )
        connection.commit()
    finally:
        connection.close()
    payload = local_task_status(local_id)
    if local_status not in TERMINAL_STATUSES:
        payload["cancel_requested"] = True
        payload["warning"] = "上游取消可能仍在处理，已开始的任务可能继续执行并计费"
    return payload, 0


def task_payload(
    task: dict[str, Any],
    request_id: str | None,
    *,
    files: list[str] | None = None,
    local_id: str | None = None,
) -> dict[str, Any]:
    status = task.get("status")
    payload: dict[str, Any] = {
        "ok": status == "succeeded" or status not in TERMINAL_STATUSES,
        "task_id": task.get("id"),
        "status": status,
        "model": task.get("model"),
        "result_quality": task.get("result_quality"),
        "billing": task.get("billing"),
        "request_id": request_id,
        "schema_version": 1,
        "local_id": local_id,
    }
    if files is not None:
        payload["files"] = files
    if status in TERMINAL_STATUSES and status != "succeeded":
        payload["ok"] = False
        payload["error"] = task.get("error") or {
            "code": f"task_{status}",
            "message": f"Task ended with status {status}",
        }
    if status in {"queued", "running", "delayed", "canceling", "cancel_requested"}:
        payload["next_action"] = "task poll"
    return {key: value for key, value in payload.items() if value is not None}


def _update_local_snapshot(
    local_id: str,
    task: dict[str, Any],
    request_id: str | None,
) -> None:
    status = task.get("status")
    if not isinstance(status, str):
        raise AsxError("Asynx returned an unknown task status", code="invalid_task_status")
    connection = connect_ledger_db()
    try:
        current = get_ledger_task(connection, local_task_id=local_id)
        if current is None:
            raise AsxError(f"本地 Task 不存在：{local_id}", code="task_not_found")
        previous = str(current.get("status"))
        update_task(
            connection,
            local_id,
            status=status,
            remote=task,
            error=task.get("error") if status in {"failed", "timeout", "canceled"} else None,
            billing=task.get("billing"),
            last_polled=utc_now(),
        )
        if status != previous:
            append_event(
                connection,
                local_id,
                "status_changed",
                {"from": previous, "to": status, "request_id": request_id},
            )
        connection.commit()
    finally:
        connection.close()


def run_submission(
    client: AsynxClient, args: argparse.Namespace, task_type: str
) -> tuple[dict[str, Any], int]:
    started_at = time.perf_counter()
    log(f"正在准备 {task_type} 请求")
    references = args.reference if task_type == "image.generate" else args.image
    from_task = getattr(args, "from_task", None)
    if task_type == "image.edit" and from_task:
        if references:
            raise AsxError(
                "Use either --image or --from-task, not both", code="conflicting_edit_inputs"
            )
        connection = connect_ledger_db()
        try:
            references = ledger_task_paths(connection, from_task)
        finally:
            connection.close()
    body, model, catalog_request_id = build_task(
        client,
        task_type=task_type,
        model_selector=args.model,
        prompt=args.prompt,
        image_size=args.image_size,
        aspect_ratio=args.aspect_ratio,
        quality=args.quality,
        count=args.count,
        output_format=args.output_format,
        references=references,
        mask=args.mask if task_type == "image.edit" else None,
        keep_reference_original=args.keep_reference_original,
    )
    prepared_at = time.perf_counter()
    input_warnings = task_input_warnings(body)
    for warning in input_warnings:
        log(f"提示：{warning['message']}")
    idempotency_key = validate_idempotency_key(
        args.idempotency_key or f"asx-{secrets.token_hex(16)}"
    )
    local_row = _persist_task_intent(
        client,
        body,
        operation=task_type.removeprefix("image."),
        model=model,
        idempotency_key=idempotency_key,
        output_dir=args.output_dir,
    )
    local_id = str(local_row["local_id"])
    output_dir = str(local_row.get("output_dir") or args.output_dir)
    log(f"提交 {task_type}，模型：{model}")
    log(f"幂等键：{idempotency_key}")
    submitted_at = time.perf_counter()
    try:
        if local_row.get("remote_task_id"):
            task, request_id = client.task(str(local_row["remote_task_id"]))
        else:
            task, request_id = client.submit(body, idempotency_key)
            _bind_submission(client, local_id, task, request_id)
    except AsxError as exc:
        _record_submit_error(local_id, exc)
        if exc.details is None:
            exc.details = {}
        if isinstance(exc.details, dict):
            exc.details.setdefault("local_id", local_id)
            exc.details.setdefault("idempotency_key", idempotency_key)
            if catalog_request_id:
                exc.details.setdefault("catalog_request_id", catalog_request_id)
            if input_warnings:
                exc.details.setdefault("warnings", input_warnings)
        raise
    accepted_at = time.perf_counter()
    timings: dict[str, Any] = {
        "prepare_seconds": round(prepared_at - started_at, 3),
        "submit_seconds": round(accepted_at - submitted_at, 3),
    }
    if args.detach:
        payload = task_payload(task, request_id, local_id=local_id)
        payload["idempotency_key"] = idempotency_key
        payload["timings"] = timings
        if input_warnings:
            payload["warnings"] = input_warnings
        return payload, 0
    task, request_id = wait_for_terminal(client, task, request_id)
    _update_local_snapshot(local_id, task, request_id)
    finished_at = time.perf_counter()
    timings["wait_seconds"] = round(finished_at - accepted_at, 3)
    if task.get("status") != "succeeded":
        payload = task_payload(task, request_id, local_id=local_id)
        payload["timings"] = timings
        if input_warnings:
            payload["warnings"] = input_warnings
        return payload, 4
    asset_metrics: list[dict[str, Any]] = []
    files = download_assets(
        client, task, output_dir, metrics=asset_metrics, local_task_id=local_id
    )
    timings["download_seconds"] = round(time.perf_counter() - finished_at, 3)
    timings["asset_downloads"] = asset_metrics
    payload = task_payload(task, request_id, files=files, local_id=local_id)
    payload["timings"] = timings
    if input_warnings:
        payload["warnings"] = input_warnings
    return payload, 0


def models_payload(client: AsynxClient, operation: str) -> dict[str, Any]:
    models, request_id = client.models()
    cache_models(client.base_url, models)
    task_type = None if operation == "all" else f"image.{operation}"
    items = []
    for model in models:
        if task_type and task_type not in model.get("task_types", []):
            continue
        items.append(
            {
                key: model.get(key)
                for key in (
                    "name",
                    "task_types",
                    "capabilities",
                    "pricing",
                    "official_discount_percent",
                )
                if model.get(key) is not None
            }
        )
    payload: dict[str, Any] = {"ok": True, "models": items}
    if request_id:
        payload["request_id"] = request_id
    return payload


def history_payload(
    client: AsynxClient,
    *,
    status: str | None,
    model: str | None,
    limit: int,
) -> dict[str, Any]:
    data, request_id = client.list_tasks(status=status, model=model, limit=limit)
    items = data.get("items")
    if not isinstance(items, list):
        raise AsxError("Task history response is invalid", code="invalid_api_response")
    payload: dict[str, Any] = {"ok": True, "tasks": items}
    next_cursor = data.get("next_cursor")
    if next_cursor:
        payload["next_cursor"] = next_cursor
    if request_id:
        payload["request_id"] = request_id
    return payload


def wait_and_download(
    client: AsynxClient, task_id: str, output_dir: str = DEFAULT_OUTPUT_DIR
) -> tuple[dict[str, Any], int]:
    ledger_connection = connect_ledger_db()
    try:
        local_row = get_ledger_task(ledger_connection, local_task_id=task_id)
        if local_row is None:
            local_row = get_ledger_task(ledger_connection, remote_task_id=task_id)
    finally:
        ledger_connection.close()
    if local_row is not None:
        return sync_local_task(client, str(local_row["local_id"]), output_dir=output_dir)
    task, request_id = client.task(task_id)
    task, request_id = wait_for_terminal(client, task, request_id)
    if task.get("status") != "succeeded":
        return task_payload(task, request_id), 4
    download_started = time.perf_counter()
    asset_metrics: list[dict[str, Any]] = []
    files = download_assets(client, task, output_dir, metrics=asset_metrics)
    payload = task_payload(task, request_id, files=files)
    payload["timings"] = {
        "download_seconds": round(time.perf_counter() - download_started, 3),
        "asset_downloads": asset_metrics,
    }
    artifact_warning = index_downloaded_artifacts(task, files)
    if artifact_warning:
        payload["warnings"] = [artifact_warning]
    return payload, 0
