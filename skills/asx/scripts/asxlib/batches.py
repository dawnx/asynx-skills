from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from .client import AsynxClient
from .constants import (
    DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
    DEFAULT_OUTPUT_DIR,
    KNOWN_STATUSES,
    LOCAL_ITEM_STATUSES,
    MAX_BATCH_ITEMS,
    UTC,
)
from .errors import AsxError
from .images import build_task, task_input_warnings
from .output import log
from .state import (
    append_event,
    bind_remote_task,
    create_task_intent,
    ensure_batch_schema,
)
from .state import connect_db as connect_state_db
from .state import (
    get_task as get_state_task,
)
from .state import (
    update_task as update_state_task,
)
from .tasks import download_assets, index_downloaded_artifacts


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect_db() -> sqlite3.Connection:
    connection = connect_state_db()
    ensure_batch_schema(connection)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute("PRAGMA synchronous = NORMAL")
    except sqlite3.DatabaseError:
        pass
    return connection


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _new_batch_id() -> str:
    return f"batch_{secrets.token_hex(6)}"


def _item_id(batch_id: str, sequence: int) -> str:
    return f"item_{batch_id.removeprefix('batch_')}_{sequence:06d}"


def _item_key(batch_id: str, sequence: int) -> str:
    return f"asx-{batch_id}-{sequence:06d}"


def batch_summary(connection: sqlite3.Connection, batch: sqlite3.Row) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT status, COUNT(*) AS count FROM batch_items WHERE batch_id = ? GROUP BY status",
        (batch["id"],),
    ).fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    total = sum(counts.values())
    pending_download = int(
        connection.execute(
            "SELECT COUNT(*) FROM batch_items WHERE batch_id = ? "
            "AND status = 'succeeded' AND files_json IS NULL",
            (batch["id"],),
        ).fetchone()[0]
    )
    finished = (
        sum(counts.get(status, 0) for status in ("succeeded", "failed", "timeout", "canceled"))
        - pending_download
    )
    return {
        "schema_version": 1,
        "id": batch["id"],
        "name": batch["name"],
        "operation": batch["operation"],
        "status": batch["status"],
        "model": batch["model"],
        "output_dir": batch["output_dir"],
        "created_at": batch["created_at"],
        "updated_at": batch["updated_at"],
        "total": total,
        "finished": finished,
        "pending_download": pending_download,
        "counts": counts,
        "cancel_requested": bool(batch["cancel_requested"]),
    }


def get_batch(connection: sqlite3.Connection, batch_id: str | None) -> sqlite3.Row:
    if batch_id:
        row = connection.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
    else:
        active = connection.execute(
            "SELECT * FROM batches WHERE status IN ('active', 'paused') "
            "ORDER BY updated_at DESC LIMIT 2"
        ).fetchall()
        if len(active) > 1:
            raise AsxError(
                "存在多个活动批次，请明确指定批次 ID",
                code="ambiguous_batch",
                details={"batches": [item["id"] for item in active]},
            )
        row = active[0] if active else connection.execute(
            "SELECT * FROM batches ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise AsxError(f"Batch not found: {batch_id or 'active batch'}", code="batch_not_found")
    return cast(sqlite3.Row, row)


def _template(batch: sqlite3.Row) -> dict[str, Any]:
    try:
        body = json.loads(batch["template_json"])
    except json.JSONDecodeError as exc:
        raise AsxError("Batch template is corrupted", code="invalid_batch_state") from exc
    if not isinstance(body, dict):
        raise AsxError("Batch template is invalid", code="invalid_batch_state")
    return body


def _insert_items(
    connection: sqlite3.Connection,
    batch_id: str,
    body: dict[str, Any],
    *,
    first_sequence: int,
    total: int,
    now: str,
    inherit_template: bool = False,
) -> None:
    for sequence in range(first_sequence, first_sequence + total):
        item_body = {"_asx_inherit_template": True} if inherit_template else json.loads(_json(body))
        item_body["metadata"] = {
            "asx_batch_id": batch_id,
            "asx_item_id": _item_id(batch_id, sequence),
        }
        connection.execute(
            "INSERT INTO batch_items "
            "(batch_id, sequence, body_json, idempotency_key, status, updated_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (batch_id, sequence, _json(item_body), _item_key(batch_id, sequence), now),
        )


def create_batch(
    client: AsynxClient,
    *,
    operation: str,
    prompt: str,
    model_selector: str | None,
    image_size: str,
    aspect_ratio: str,
    quality: str,
    count: int,
    output_format: str,
    references: list[str],
    mask: str | None,
    total: int,
    name: str | None,
    output_dir: str | None,
    keep_reference_original: bool = False,
) -> dict[str, Any]:
    if not 1 <= total <= MAX_BATCH_ITEMS:
        raise AsxError(
            f"Batch size must be between 1 and {MAX_BATCH_ITEMS}", code="invalid_batch_size"
        )
    body, selected_model, catalog_request_id = build_task(
        client,
        task_type=f"image.{operation}",
        model_selector=model_selector,
        prompt=prompt,
        image_size=image_size,
        aspect_ratio=aspect_ratio,
        quality=quality,
        count=count,
        output_format=output_format,
        references=references,
        mask=mask,
        keep_reference_original=keep_reference_original,
    )
    batch_id = _new_batch_id()
    now = utc_now()
    resolved_output_dir = str(
        (Path(output_dir).expanduser() if output_dir else Path(DEFAULT_OUTPUT_DIR) / batch_id).resolve()
    )
    batch_name = name.strip() if name and name.strip() else batch_id
    connection = connect_db()
    try:
        connection.execute(
            "INSERT INTO batches "
            "(id, operation, name, status, model, output_dir, template_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                batch_id,
                operation,
                batch_name,
                selected_model,
                resolved_output_dir,
                _json(body),
                now,
                now,
            ),
        )
        _insert_items(
            connection,
            batch_id,
            body,
            first_sequence=1,
            total=total,
            now=now,
            inherit_template=True,
        )
        connection.commit()
        summary = batch_summary(connection, get_batch(connection, batch_id))
    except sqlite3.Error as exc:
        connection.rollback()
        raise AsxError(
            f"Cannot create local batch state: {exc}", code="batch_state_write_failed"
        ) from exc
    finally:
        connection.close()
    if catalog_request_id:
        summary["catalog_request_id"] = catalog_request_id
    input_warnings = task_input_warnings(body)
    if input_warnings:
        summary["warnings"] = input_warnings
    return summary


def add_to_batch(
    client: AsynxClient,
    batch_id: str | None,
    *,
    total: int,
    prompt: str | None,
    references: list[str] | None,
    mask: str | None,
    count: int | None,
    image_size: str | None,
    aspect_ratio: str | None,
    quality: str | None,
    output_format: str | None,
    keep_reference_original: bool = False,
) -> dict[str, Any]:
    if not 1 <= total <= MAX_BATCH_ITEMS:
        raise AsxError(
            f"Added item count must be between 1 and {MAX_BATCH_ITEMS}",
            code="invalid_batch_size",
        )
    connection = connect_db()
    try:
        batch = get_batch(connection, batch_id)
        if batch["status"] not in {"active", "paused"}:
            raise AsxError(
                "Cannot append to a completed or canceled batch", code="batch_not_appendable"
            )
        body = _template(batch)
        has_overrides = any(
            value is not None
            for value in (
                prompt,
                references,
                mask,
                count,
                image_size,
                aspect_ratio,
                quality,
                output_format,
            )
        )
        if has_overrides:
            input_value = body.get("input")
            if not isinstance(input_value, dict):
                raise AsxError("Batch template has invalid input", code="invalid_batch_state")
            existing_references = (
                input_value.get("reference_images")
                if batch["operation"] == "generate"
                else input_value.get("images")
            )
            if not isinstance(existing_references, list) or not all(
                isinstance(item, str) for item in existing_references
            ):
                raise AsxError(
                    "Batch template has invalid image inputs", code="invalid_batch_state"
                )
            body, _selected_model, _request_id = build_task(
                client,
                task_type=f"image.{batch['operation']}",
                model_selector=batch["model"],
                prompt=prompt if prompt is not None else str(input_value.get("prompt", "")),
                image_size=image_size or str(input_value.get("image_size", "1K")),
                aspect_ratio=aspect_ratio or str(input_value.get("aspect_ratio", "1:1")),
                quality=quality or str(input_value.get("quality", "standard")),
                count=count or int(input_value.get("count", 1)),
                output_format=output_format or str(input_value.get("output_format", "png")),
                references=references if references is not None else existing_references,
                mask=(
                    mask
                    if mask is not None
                    else str(input_value.get("mask"))
                    if batch["operation"] == "edit" and input_value.get("mask")
                    else None
                ),
                keep_reference_original=(
                    keep_reference_original if references is not None else True
                ),
            )
        connection.execute("BEGIN IMMEDIATE")
        max_sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM batch_items WHERE batch_id = ?",
                (batch["id"],),
            ).fetchone()[0]
        )
        now = utc_now()
        _insert_items(
            connection,
            batch["id"],
            body,
            first_sequence=max_sequence + 1,
            total=total,
            now=now,
            inherit_template=not has_overrides,
        )
        connection.execute(
            "UPDATE batches SET updated_at = ? WHERE id = ?", (now, batch["id"])
        )
        connection.commit()
        summary = batch_summary(connection, get_batch(connection, batch["id"]))
        input_warnings = task_input_warnings(body)
        if input_warnings:
            summary["warnings"] = input_warnings
        return summary
    except sqlite3.Error as exc:
        connection.rollback()
        raise AsxError(
            f"Cannot append to local batch state: {exc}", code="batch_state_write_failed"
        ) from exc
    finally:
        connection.close()


def list_batches() -> dict[str, Any]:
    connection = connect_db()
    try:
        rows = connection.execute("SELECT * FROM batches ORDER BY updated_at DESC LIMIT 100").fetchall()
        return {"ok": True, "batches": [batch_summary(connection, row) for row in rows]}
    finally:
        connection.close()


def batch_status(batch_id: str | None, *, include_items: bool = False) -> dict[str, Any]:
    connection = connect_db()
    try:
        batch = get_batch(connection, batch_id)
        summary = batch_summary(connection, batch)
        if include_items:
            items = connection.execute(
                "SELECT sequence, status, task_id, files_json, error_json, updated_at "
                "FROM batch_items WHERE batch_id = ? ORDER BY sequence LIMIT 1000",
                (batch["id"],),
            ).fetchall()
            summary["items"] = [
                {
                    "sequence": row["sequence"],
                    "status": row["status"],
                    "task_id": row["task_id"],
                    "files": json.loads(row["files_json"]) if row["files_json"] else [],
                    "error": json.loads(row["error_json"]) if row["error_json"] else None,
                    "updated_at": row["updated_at"],
                }
                for row in items
            ]
        return {"ok": True, "batch": summary}
    finally:
        connection.close()


def _update_item(
    connection: sqlite3.Connection,
    item_id: int,
    *,
    status: str,
    task_id: str | None = None,
    request_id: str | None = None,
    files: list[str] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    if status not in LOCAL_ITEM_STATUSES:
        raise AsxError(f"Invalid local batch item status: {status}", code="invalid_batch_state")
    connection.execute(
        "UPDATE batch_items SET status = ?, task_id = COALESCE(?, task_id), "
        "request_id = COALESCE(?, request_id), files_json = COALESCE(?, files_json), "
        "error_json = ?, updated_at = ? WHERE id = ?",
        (
            status,
            task_id,
            request_id,
            _json(files) if files is not None else None,
            _json(error) if error is not None else None,
            utc_now(),
            item_id,
        ),
    )


def _refresh_batch_status(connection: sqlite3.Connection, batch_id: str) -> None:
    total = int(
        connection.execute(
            "SELECT COUNT(*) FROM batch_items WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
    )
    unfinished = int(
        connection.execute(
            "SELECT COUNT(*) FROM batch_items WHERE batch_id = ? AND "
            "(status NOT IN ('succeeded', 'failed', 'timeout', 'canceled') "
            "OR (status = 'succeeded' AND files_json IS NULL))",
            (batch_id,),
        ).fetchone()[0]
    )
    if total and unfinished == 0:
        connection.execute(
            "UPDATE batches SET status = CASE WHEN cancel_requested = 1 "
            "THEN 'canceled' ELSE 'completed' END, updated_at = ? WHERE id = ?",
            (utc_now(), batch_id),
        )
    else:
        connection.execute("UPDATE batches SET updated_at = ? WHERE id = ?", (utc_now(), batch_id))


def poll_batch(
    client: AsynxClient,
    batch_id: str | None,
    *,
    submissions_limit: int = DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
) -> dict[str, Any]:
    if not 1 <= submissions_limit <= 50:
        raise AsxError("Submission limit must be between 1 and 50", code="invalid_submission_limit")
    connection = connect_db()
    try:
        batch = get_batch(connection, batch_id)
        if batch["status"] == "paused":
            return {"ok": True, "batch": batch_summary(connection, batch), "paused": True}
        if batch["status"] == "completed" or (
            batch["status"] == "canceled" and not batch["cancel_requested"]
        ):
            return {"ok": True, "batch": batch_summary(connection, batch), "finished": True}
        stale_before = datetime.now(UTC) - timedelta(minutes=5)
        connection.execute(
            "UPDATE batch_items SET status = 'pending', updated_at = ? "
            "WHERE batch_id = ? AND status = 'submitting' AND updated_at < ?",
            (utc_now(), batch["id"], stale_before.isoformat(timespec="seconds")),
        )
        connection.commit()

        submitted = 0
        artifact_warnings: list[dict[str, Any]] = []
        asset_downloads: list[dict[str, Any]] = []
        pending = connection.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? AND status = 'pending' "
            "ORDER BY sequence LIMIT ?",
            (batch["id"], submissions_limit),
        ).fetchall()
        for item in pending:
            claim = connection.execute(
                "UPDATE batch_items SET status = 'submitting', error_json = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (utc_now(), item["id"]),
            )
            if claim.rowcount != 1:
                continue
            connection.commit()
            try:
                body = json.loads(item["body_json"])
                if body.pop("_asx_inherit_template", False):
                    template = _template(batch)
                    template["metadata"] = body.get("metadata", {})
                    body = template
                local_task_id = f"batchitem_{int(item['id'])}"
                intent = create_task_intent(
                    connection,
                    operation=str(batch["operation"]),
                    model=str(batch["model"]),
                    idempotency_key=str(item["idempotency_key"]),
                    request=body,
                    output_dir=str(batch["output_dir"]),
                    base_url=client.base_url,
                    local_id=local_task_id,
                    status="submitting",
                )
                connection.commit()
                if intent.get("remote_task_id"):
                    task, request_id = client.task(str(intent["remote_task_id"]))
                else:
                    task, request_id = client.submit(body, item["idempotency_key"])
                    bind_remote_task(
                        connection,
                        local_task_id,
                        str(task.get("id")),
                        remote=task,
                        status=(
                            task.get("status")
                            if isinstance(task.get("status"), str)
                            else "queued"
                        ),
                        last_polled=utc_now(),
                    )
                    append_event(
                        connection,
                        local_task_id,
                        "submitted",
                        {"request_id": request_id},
                    )
                submitted_task_id = task.get("id")
                status = task.get("status") if isinstance(task.get("status"), str) else "queued"
                if not isinstance(submitted_task_id, str):
                    raise AsxError("Task response is missing an ID", code="invalid_task_response")
                state_task = get_state_task(connection, local_task_id=local_task_id)
                if state_task is not None:
                    previous_state = str(state_task.get("status"))
                    update_state_task(
                        connection,
                        local_task_id,
                        status=status,
                        remote=task,
                        billing=task.get("billing"),
                        last_polled=utc_now(),
                    )
                    if previous_state != status:
                        append_event(
                            connection,
                            local_task_id,
                            "status_changed",
                            {"from": previous_state, "to": status, "request_id": request_id},
                        )
                _update_item(
                    connection,
                    int(item["id"]),
                    status=status if status in LOCAL_ITEM_STATUSES else "submitted",
                    task_id=submitted_task_id,
                    request_id=request_id,
                )
                connection.commit()
                submitted += 1
            except AsxError as exc:
                _update_item(
                    connection,
                    int(item["id"]),
                    status=(
                        "failed"
                        if exc.http_status in {400, 404, 410, 422}
                        else "pending"
                    ),
                    error=exc.payload().get("error"),
                )
                connection.commit()
                log(f"批次 {batch['id']} 第 {item['sequence']} 项暂未提交：{exc.message}")

        remote_items = connection.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? AND status IN "
            "('submitted', 'queued', 'running', 'delayed', 'canceling', 'succeeded') "
            "ORDER BY sequence",
            (batch["id"],),
        ).fetchall()
        downloaded = 0
        for item in remote_items:
            if item["status"] == "succeeded" and item["files_json"]:
                try:
                    cached_files = json.loads(item["files_json"])
                except json.JSONDecodeError:
                    cached_files = []
                if isinstance(cached_files, list) and cached_files and all(
                    isinstance(path, str) and Path(path).is_file() for path in cached_files
                ):
                    continue
            remote_task_id: str | None = item["task_id"]
            if not isinstance(remote_task_id, str):
                continue
            state_task_row = get_state_task(connection, remote_task_id=remote_task_id)
            batch_local_task_id = str(state_task_row["local_id"]) if state_task_row else None
            try:
                task, request_id = client.task(remote_task_id)
            except AsxError as exc:
                permanent = exc.http_status in {400, 404, 410, 422}
                if state_task_row is not None and permanent:
                    update_state_task(
                        connection,
                        str(state_task_row["local_id"]),
                        status="failed",
                        error=exc.payload().get("error"),
                        last_polled=utc_now(),
                    )
                    append_event(
                        connection,
                        str(state_task_row["local_id"]),
                        "task_error",
                        exc.payload().get("error"),
                    )
                _update_item(
                    connection,
                    int(item["id"]),
                    status="failed" if permanent else item["status"],
                    error=exc.payload().get("error"),
                )
                connection.commit()
                continue
            status = task.get("status")
            if not isinstance(status, str) or status not in KNOWN_STATUSES:
                _update_item(
                    connection,
                    int(item["id"]),
                    status=item["status"],
                    request_id=request_id,
                    error={
                        "code": "invalid_task_status",
                        "message": "Asynx returned an unknown task status",
                    },
                )
                connection.commit()
                continue
            if state_task_row is not None:
                previous_state = str(state_task_row.get("status"))
                update_state_task(
                    connection,
                    str(state_task_row["local_id"]),
                    status=status,
                    remote=task,
                    billing=task.get("billing"),
                    error=task.get("error") if status in {"failed", "timeout", "canceled"} else None,
                    last_polled=utc_now(),
                )
                if previous_state != status:
                    append_event(
                        connection,
                        str(state_task_row["local_id"]),
                        "status_changed",
                        {"from": previous_state, "to": status, "request_id": request_id},
                    )
                connection.commit()
            files: list[str] | None = None
            if status == "succeeded":
                files = json.loads(item["files_json"]) if item["files_json"] else None
                if not files:
                    try:
                        files = download_assets(
                            client,
                            task,
                            batch["output_dir"],
                            prefix=f"{batch['id']}-{int(item['sequence']):06d}",
                            metrics=asset_downloads,
                            local_task_id=batch_local_task_id,
                        )
                        downloaded += len(files)
                    except AsxError as exc:
                        _update_item(
                            connection,
                            int(item["id"]),
                            status="succeeded",
                            request_id=request_id,
                            error=exc.payload().get("error"),
                        )
                        connection.commit()
                        continue
                if files and not batch_local_task_id:
                    artifact_warning = index_downloaded_artifacts(
                        task, files, operation=batch["operation"]
                    )
                    if artifact_warning:
                        artifact_warnings.append(artifact_warning)
                        log(f"提示：{artifact_warning['message']}")
            _update_item(
                connection,
                int(item["id"]),
                status=status,
                request_id=request_id,
                files=files,
                error=(
                    task.get("error")
                    if status in {"failed", "timeout", "canceled"}
                    else None
                ),
            )
            connection.commit()
        _refresh_batch_status(connection, batch["id"])
        connection.commit()
        result = batch_summary(connection, get_batch(connection, batch["id"]))
        result["submitted_now"] = submitted
        result["downloaded_now"] = downloaded
        if asset_downloads:
            result["asset_downloads_now"] = asset_downloads
        if artifact_warnings:
            result["warnings"] = artifact_warnings
        if result["counts"].get("failed", 0):
            result["partial"] = True
            result["action_required"] = "查看 batch status --items，处理失败项"
        return {"ok": not bool(result.get("partial")), "batch": result}
    finally:
        connection.close()


def set_batch_paused(batch_id: str | None, paused: bool) -> dict[str, Any]:
    connection = connect_db()
    try:
        batch = get_batch(connection, batch_id)
        if batch["status"] in {"completed", "canceled"}:
            raise AsxError("The batch has already finished", code="batch_finished")
        status = "paused" if paused else "active"
        connection.execute(
            "UPDATE batches SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), batch["id"]),
        )
        connection.commit()
        return {"ok": True, "batch": batch_summary(connection, get_batch(connection, batch["id"]))}
    finally:
        connection.close()


def cancel_batch(client: AsynxClient, batch_id: str | None) -> dict[str, Any]:
    connection = connect_db()
    try:
        batch = get_batch(connection, batch_id)
        if batch["status"] == "completed":
            return {"ok": True, "batch": batch_summary(connection, batch), "finished": True}
        connection.execute(
            "UPDATE batch_items SET status = 'canceled', error_json = NULL, updated_at = ? "
            "WHERE batch_id = ? AND status IN ('pending', 'submitting')",
            (utc_now(), batch["id"]),
        )
        connection.commit()
        remote = connection.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? AND task_id IS NOT NULL "
            "AND status IN ('submitted', 'queued', 'running', 'delayed', 'canceling')",
            (batch["id"],),
        ).fetchall()
        for item in remote:
            try:
                task, request_id = client.cancel(str(item["task_id"]))
                status = task.get("status") if isinstance(task.get("status"), str) else "canceling"
                _update_item(
                    connection,
                    int(item["id"]),
                    status=cast(str, status),
                    request_id=request_id,
                )
            except AsxError as exc:
                _update_item(
                    connection,
                    int(item["id"]),
                    status=item["status"],
                    error=exc.payload().get("error"),
                )
            connection.commit()
        connection.execute(
            "UPDATE batches SET cancel_requested = 1, updated_at = ? WHERE id = ?",
            (utc_now(), batch["id"]),
        )
        _refresh_batch_status(connection, batch["id"])
        connection.commit()
        summary = batch_summary(connection, get_batch(connection, batch["id"]))
        payload: dict[str, Any] = {"ok": True, "batch": summary}
        if summary["status"] != "canceled":
            payload["cancel_requested"] = True
            payload["warning"] = "上游取消可能仍在处理，已开始的任务可能继续执行并计费"
        else:
            payload["finished"] = True
        return payload
    finally:
        connection.close()


def wait_for_batch(
    client: AsynxClient,
    batch_id: str | None,
    *,
    interval: float,
    max_wait: float,
) -> dict[str, Any]:
    if interval < 1 or interval > 300:
        raise AsxError(
            "Polling interval must be between 1 and 300 seconds", code="invalid_poll_interval"
        )
    if max_wait < 0:
        raise AsxError("Maximum wait time cannot be negative", code="invalid_max_wait")
    started = time.monotonic()
    while True:
        result = poll_batch(client, batch_id)
        batch = result["batch"]
        if batch["status"] in {"completed", "canceled"}:
            return result
        if max_wait and time.monotonic() - started >= max_wait:
            result["wait_timeout"] = True
            return result
        time.sleep(interval)


def execute_batch(
    client: AsynxClient | None, args: argparse.Namespace
) -> tuple[dict[str, Any], int]:
    action = args.batch_command
    if action == "list":
        return list_batches(), 0
    if action == "status":
        if args.refresh:
            if client is None:
                raise AsxError("查询批次需要 API Key", code="missing_api_key")
            poll_batch(client, args.batch_id)
        return batch_status(args.batch_id, include_items=args.items), 0
    if action in {"pause", "resume"}:
        return set_batch_paused(args.batch_id, paused=action == "pause"), 0
    if client is None:
        raise AsxError("此批次操作需要 API Key", code="missing_api_key")
    if action == "poll":
        return poll_batch(client, args.batch_id, submissions_limit=args.limit), 0
    if action == "wait":
        return wait_for_batch(
            client, args.batch_id, interval=args.interval, max_wait=args.max_wait
        ), 0
    if action == "cancel":
        return cancel_batch(client, args.batch_id), 0
    if action == "create":
        log("正在创建并提交图片批次")
        references = args.reference if args.operation == "generate" else args.image
        if args.operation == "edit" and not references:
            raise AsxError("编辑批次至少需要一张输入图片", code="missing_edit_image")
        created = create_batch(
            client,
            operation=args.operation,
            prompt=args.prompt,
            model_selector=args.model,
            image_size=args.image_size,
            aspect_ratio=args.aspect_ratio,
            quality=args.quality,
            count=args.count,
            output_format=args.output_format,
            references=references,
            mask=args.mask,
            total=args.total,
            name=args.name,
            output_dir=args.output_dir,
            keep_reference_original=args.keep_reference_original,
        )
        for warning in created.get("warnings", []):
            log(f"提示：{warning['message']}")
        progressed = poll_batch(client, created["id"], submissions_limit=args.limit)
        progressed["created"] = True
        if created.get("warnings"):
            progressed["warnings"] = created["warnings"]
        return progressed, 0
    if action == "add":
        references = args.reference if args.reference else args.image if args.image else None
        result = add_to_batch(
            client,
            args.batch_id,
            total=args.total,
            prompt=args.prompt,
            references=references,
            mask=args.mask,
            count=args.count,
            image_size=args.image_size,
            aspect_ratio=args.aspect_ratio,
            quality=args.quality,
            output_format=args.output_format,
            keep_reference_original=args.keep_reference_original,
        )
        for warning in result.get("warnings", []):
            log(f"提示：{warning['message']}")
        progressed = poll_batch(client, result["id"], submissions_limit=args.limit)
        progressed["added"] = args.total
        if result.get("warnings"):
            progressed["warnings"] = result["warnings"]
        return progressed, 0
    raise AssertionError("unreachable")
