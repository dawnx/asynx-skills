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
from .errors import AsxError, friendly_task_error
from .image_options import IMAGE_OPTIONS, options_from_args
from .image_results import requested_aspect_ratio
from .images import build_task, task_input_warnings
from .media_results import attach_media_details
from .output import log
from .reference_media import validate_request_body
from .state import (
    append_event,
    bind_remote_task,
    cancel_blocked_batch_intent,
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
from .videos import _media_url, build_video_task, prepare_video_image


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


def _available_files(raw: str | None) -> list[str]:
    try:
        files = json.loads(raw) if raw else None
    except (ValueError, TypeError):
        return []
    if isinstance(files, list) and files and all(
        isinstance(path, str) and Path(path).is_file() for path in files
    ):
        return cast(list[str], files)
    return []


def _pending_downloads(connection: sqlite3.Connection, batch_id: str) -> int:
    return sum(
        not _completed_item_files(row)
        for row in connection.execute(
            "SELECT files_json, error_json FROM batch_items WHERE batch_id = ? AND status = 'succeeded'",
            (batch_id,),
        )
    )


def _completed_item_files(item: sqlite3.Row) -> list[str]:
    # files_json may contain partial downloads; a saved download error keeps the
    # item incomplete until all assets are retrieved and the error is cleared.
    return [] if item["error_json"] else _available_files(item["files_json"])


def batch_summary(connection: sqlite3.Connection, batch: sqlite3.Row) -> dict[str, Any]:
    rows = connection.execute(
        "SELECT status, COUNT(*) AS count FROM batch_items WHERE batch_id = ? GROUP BY status",
        (batch["id"],),
    ).fetchall()
    counts = {str(row["status"]): int(row["count"]) for row in rows}
    total = sum(counts.values())
    pending_download = _pending_downloads(connection, str(batch["id"]))
    unsuccessful = sum(counts.get(status, 0) for status in ("failed", "timeout", "canceled"))
    finished = (
        sum(counts.get(status, 0) for status in ("succeeded", "failed", "timeout", "canceled"))
        - pending_download
    )
    return {
        "schema_version": 1,
        "id": batch["id"],
        "name": batch["name"],
        "operation": batch["operation"],
        "task_type": _template(batch).get("task_type", f"image.{batch['operation']}"),
        "modality": "video" if _template(batch).get("task_type") == "video.generate" else "image",
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
        "partial": bool(unsuccessful),
        **(
            {
                "next_action_args": ["batch", "status", batch["id"], "--refresh"],
                "action_required": "批次已暂停。可刷新已提交任务的状态；如需继续提交剩余项目，请先恢复批次。",
            }
            if batch["status"] == "paused"
            else {"next_action_args": ["batch", "wait", batch["id"]]}
            if finished < total
            else {}
        ),
        **(
            {"action_required": "请查看批次明细，处理失败、超时或已取消的项目。"}
            if unsuccessful and batch["status"] != "paused" else {}
        ),
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
                "当前存在多个进行中的批次，请指定批次 ID。",
                code="ambiguous_batch",
                details={"batches": [item["id"] for item in active]},
            )
        row = active[0] if active else connection.execute(
            "SELECT * FROM batches ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        raise AsxError(
            f"未找到批次 {batch_id}，请检查批次 ID。" if batch_id else "暂无可用的批次，请先创建批次。",
            code="batch_not_found",
        )
    return cast(sqlite3.Row, row)


def _template(batch: sqlite3.Row) -> dict[str, Any]:
    try:
        body = json.loads(batch["template_json"])
    except json.JSONDecodeError as exc:
        raise AsxError("批次设置读取失败，请保留现有记录并联系服务支持。", code="invalid_batch_state") from exc
    if not isinstance(body, dict):
        raise AsxError("批次设置不完整，无法继续操作；请保留现有记录并联系服务支持。", code="invalid_batch_state")
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


def _reject_image_options(
    image_size: str | None, quality: str | None, count: int | None,
    output_format: str | None, mask: str | None,
) -> None:
    if any(value is not None for value in (image_size, quality, output_format, mask)) or count not in (None, 1):
        raise AsxError("视频批次不支持图片尺寸、质量、蒙版、输出格式及单任务多图参数。", code="conflicting_batch_options")


def create_batch(
    client: AsynxClient,
    *,
    operation: str,
    prompt: str,
    model_selector: str | None,
    image_size: str,
    aspect_ratio: str,
    quality: str | None,
    count: int,
    output_format: str,
    references: list[str],
    mask: str | None,
    total: int,
    name: str | None,
    output_dir: str | None,
    keep_reference_original: bool = False,
    options: dict[str, Any] | None = None,
    duration: float = 5,
    resolution: str = "720p",
    first_frame: str | None = None,
    last_frame: str | None = None,
    reference_videos: list[str] | None = None,
    reference_audios: list[str] | None = None,
) -> dict[str, Any]:
    if not 1 <= total <= MAX_BATCH_ITEMS:
        raise AsxError(
            f"批次数量须为 1–{MAX_BATCH_ITEMS}，请调整 --total。", code="invalid_batch_size"
        )
    if operation == "video.generate":
        if options:
            raise AsxError("视频批次不支持图片高级参数。", code="conflicting_batch_options")
        _reject_image_options(image_size, quality, count, output_format, mask)
        body, selected_model, catalog_request_id = build_video_task(
            client, prompt=prompt, model_selector=model_selector, duration=duration,
            resolution=resolution, aspect_ratio=aspect_ratio or "16:9", references=references,
            first_frame=first_frame, last_frame=last_frame,
            reference_videos=reference_videos, reference_audios=reference_audios,
        )
    else:
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
            options=options,
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
                "generate" if operation == "video.generate" else operation,
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
            "批次记录保存失败，请检查数据目录的写入权限与磁盘空间。", code="batch_state_write_failed"
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
    options: dict[str, Any] | None = None,
    duration: float | None = None,
    resolution: str | None = None,
    first_frame: str | None = None,
    last_frame: str | None = None,
    reference_videos: list[str] | None = None,
    reference_audios: list[str] | None = None,
) -> dict[str, Any]:
    if not 1 <= total <= MAX_BATCH_ITEMS:
        raise AsxError(
            f"追加数量须为 1–{MAX_BATCH_ITEMS}，请调整 --total。",
            code="invalid_batch_size",
        )
    connection = connect_db()
    try:
        batch = get_batch(connection, batch_id)
        if batch["status"] not in {"active", "paused"}:
            raise AsxError(
                "该批次已结束或已取消，无法追加任务；请新建批次。", code="batch_not_appendable"
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
                duration, resolution, first_frame, last_frame, reference_videos, reference_audios,
                options or None,
            )
        )
        is_video = body.get("task_type") == "video.generate"
        if is_video and options:
            raise AsxError("视频批次不支持图片高级参数。", code="conflicting_batch_options")
        if is_video and has_overrides:
            _reject_image_options(image_size, quality, count, output_format, mask)
            value = body["input"]
            existing = value.get("inputs", [])
            body, _model, _rid = build_video_task(
                client, prompt=prompt if prompt is not None else value["prompt"], model_selector=batch["model"],
                duration=duration if duration is not None else value["duration"],
                resolution=resolution if resolution is not None else value["resolution"],
                aspect_ratio=aspect_ratio if aspect_ratio is not None else value["aspect_ratio"],
            )
            inputs: list[dict[str, str]] = []
            for kind, role, replacement in (
                ("image", "reference", references),
                ("image", "first_frame", [first_frame] if first_frame is not None else None),
                ("image", "last_frame", [last_frame] if last_frame is not None else None),
                ("video", "reference", reference_videos), ("audio", "reference", reference_audios),
            ):
                if replacement is None:
                    inputs.extend(dict(item) for item in existing if item["type"] == kind and item["role"] == role)
                else:
                    prepare = prepare_video_image if kind == "image" else _media_url
                    inputs.extend({"type": kind, "role": role, "url": prepare(source)} for source in replacement)
            if any(item["role"] == "last_frame" for item in inputs) and not any(item["role"] == "first_frame" for item in inputs):
                raise AsxError("使用尾帧时必须同时提供首帧。", code="missing_first_frame")
            body["input"]["inputs"] = inputs
            validate_request_body(body)
        elif has_overrides:
            if any(value is not None for value in (duration, resolution, first_frame, last_frame, reference_videos, reference_audios)):
                raise AsxError("图片批次不支持视频参数。", code="conflicting_batch_options")
            input_value = body.get("input")
            if not isinstance(input_value, dict):
                raise AsxError("批次输入设置读取失败，请保留现有记录并联系服务支持。", code="invalid_batch_state")
            existing_references = (
                input_value.get("reference_images")
                if batch["operation"] == "generate"
                else input_value.get("images")
            )
            if not isinstance(existing_references, list) or not all(
                isinstance(item, str) for item in existing_references
            ):
                raise AsxError(
                    "批次中的参考图片记录无效，无法继续操作；请保留现有记录并联系服务支持。", code="invalid_batch_state"
                )
            inherited_options = {key: value for key, value in input_value.items() if key in IMAGE_OPTIONS}
            if image_size is not None or aspect_ratio is not None:
                inherited_options.pop("size", None)
            inherited_options.update(options or {})
            body, _selected_model, _request_id = build_task(
                client,
                task_type=f"image.{batch['operation']}",
                model_selector=batch["model"],
                prompt=prompt if prompt is not None else str(input_value.get("prompt", "")),
                image_size=image_size or str(input_value.get("image_size", "1K")),
                aspect_ratio=aspect_ratio or str(input_value.get("aspect_ratio", "1:1")),
                quality=(
                    quality
                    if quality is not None
                    else input_value.get("quality")
                ),
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
                options=inherited_options,
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
            "追加的任务保存失败，请检查数据目录的写入权限与磁盘空间。", code="batch_state_write_failed"
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


def _item_error(row: sqlite3.Row) -> dict[str, Any] | None:
    error = json.loads(row["error_json"]) if row["error_json"] else None
    remote = json.loads(row["remote_json"]) if row["remote_json"] else {}
    # Older records may contain the original remote error.  Local download and
    # state errors already have their own actionable messages.
    if error is not None and remote.get("error") == error:
        return friendly_task_error(error, status=row["status"])
    return cast(dict[str, Any] | None, error)


def batch_status(batch_id: str | None, *, include_items: bool = False) -> dict[str, Any]:
    connection = connect_db()
    try:
        batch = get_batch(connection, batch_id)
        summary = batch_summary(connection, batch)
        _attach_batch_results(connection, batch, summary)
        if include_items:
            items = connection.execute(
                "SELECT i.sequence, i.status, i.task_id, i.files_json, i.error_json, i.updated_at, t.remote_json "
                "FROM batch_items i LEFT JOIN tasks t ON t.idempotency_key = i.idempotency_key "
                "WHERE i.batch_id = ? ORDER BY i.sequence LIMIT 1000",
                (batch["id"],),
            ).fetchall()
            summary["items"] = [
                {
                    "sequence": row["sequence"],
                    "status": row["status"],
                    "task_id": row["task_id"],
                    "files": json.loads(row["files_json"]) if row["files_json"] else [],
                    "error": _item_error(row),
                    "updated_at": row["updated_at"],
                }
                for row in items
            ]
        return {"ok": True, "batch": summary}
    finally:
        connection.close()


def _attach_batch_results(
    connection: sqlite3.Connection, batch: sqlite3.Row, summary: dict[str, Any]
) -> None:
    """Collect deliverables only when returning to the caller, never on each wait tick."""
    rows = connection.execute(
        "SELECT i.*, t.remote_json FROM batch_items i LEFT JOIN tasks t "
        "ON t.idempotency_key = i.idempotency_key WHERE i.batch_id = ? ORDER BY i.sequence",
        (batch["id"],),
    ).fetchall()
    ledger_files: dict[str, list[str]] = {}
    for asset in connection.execute(
        "SELECT t.idempotency_key, a.path FROM assets a "
        "JOIN tasks t ON t.local_id = a.local_task_id "
        "JOIN batch_items i ON i.idempotency_key = t.idempotency_key "
        "WHERE i.batch_id = ? AND a.state = 'available' ORDER BY i.sequence, a.remote_index",
        (batch["id"],),
    ):
        ledger_files.setdefault(str(asset["idempotency_key"]), []).append(str(asset["path"]))
    files: list[str] = []
    images: list[dict[str, Any]] = []
    videos: list[dict[str, Any]] = []
    media: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    download_errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    seen: set[str] = set()
    template_ratio = requested_aspect_ratio(_template(batch))
    for row in rows:
        identity = {"sequence": row["sequence"], "task_id": row["task_id"]}
        error = _item_error(row)
        if row["status"] in {"failed", "timeout", "canceled"}:
            failures.append({**identity, "status": row["status"], "error": error})
        stored_files = json.loads(row["files_json"]) if row["files_json"] else []
        stored_files = list(dict.fromkeys([
            *stored_files, *ledger_files.get(str(row["idempotency_key"]), []),
        ]))
        available = [path for path in stored_files if isinstance(path, str) and Path(path).is_file()]
        if row["status"] == "succeeded" and (error or not _available_files(row["files_json"])):
            download_errors.append({
                **identity,
                "error": error or {
                    "code": "download_pending",
                    "message": "结果已生成，但尚未完成下载；刷新或等待该批次即可继续下载，无需重新生成。",
                },
            })
        elif error and row["status"] not in {"failed", "timeout", "canceled"}:
            warnings.append({**identity, **error})
        remote = json.loads(row["remote_json"]) if row["remote_json"] else {}
        if remote.get("result_quality") == "degraded":
            warnings.append({
                **identity, "code": "result_degraded", "result_quality": "degraded",
                "message": "该结果被服务标记为降级，请确认是否符合预期。",
            })
        item_body = json.loads(row["body_json"])
        ratio = (
            template_ratio if item_body.get("_asx_inherit_template")
            else requested_aspect_ratio(item_body)
        )
        new_files = [path for path in available if path not in seen]
        seen.update(new_files)
        files.extend(new_files)
        details: dict[str, Any] = {}
        if new_files:
            attach_media_details(details, new_files, expected_ratio=ratio, task=remote)
        images.extend({**image, **identity} for image in details.get("images", []))
        videos.extend({**video, **identity} for video in details.get("videos", []))
        media.extend({**item, **identity} for item in details.get("media", []))
        warnings.extend({**warning, **identity} for warning in details.get("warnings", []))
    summary.update(files=files, images=images, videos=videos, media=media, failed_items=failures, download_errors=download_errors)
    if download_errors:
        warnings.append({
            "code": "batch_download_incomplete",
            "message": "部分结果尚未完成下载，请继续等待或刷新该批次；请勿重新提交生成任务。",
        })
    if failures:
        paused_notice = summary.get("action_required", "") if batch["status"] == "paused" else ""
        summary["action_required"] = (
            paused_notice + "请处理未成功的项目；重新生成将产生费用。"
        )
    if warnings:
        summary.setdefault("warnings", []).extend(warnings)


def _finish_batch_result(result: dict[str, Any]) -> dict[str, Any]:
    connection = connect_db()
    try:
        batch = get_batch(connection, result["batch"]["id"])
        _attach_batch_results(connection, batch, result["batch"])
        return result
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
        raise AsxError("批次进度记录异常，无法更新；请保留现有记录并联系服务支持。", code="invalid_batch_state")
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
            "status NOT IN ('succeeded', 'failed', 'timeout', 'canceled')",
            (batch_id,),
        ).fetchone()[0]
    )
    if total and unfinished == 0 and _pending_downloads(connection, batch_id) == 0:
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
    _refresh_only: bool = False,
    _include_results: bool = True,
) -> dict[str, Any]:
    if not 1 <= submissions_limit <= 50:
        raise AsxError("单次提交数量须为 1–50，请调整 --limit。", code="invalid_submission_limit")
    connection = connect_db()
    try:
        batch = get_batch(connection, batch_id)
        summary = batch_summary(connection, batch)
        if batch["status"] == "paused" and not _refresh_only:
            if _include_results:
                _attach_batch_results(connection, batch, summary)
            return {"ok": not summary["partial"], "batch": summary, "paused": True}
        if (
            batch["status"] == "completed"
            or (batch["status"] == "canceled" and not batch["cancel_requested"])
        ) and not summary["pending_download"]:
            if _include_results:
                _attach_batch_results(connection, batch, summary)
            return {"ok": not summary["partial"], "batch": summary, "finished": True}
        stale_before = datetime.now(UTC) - timedelta(minutes=5)
        connection.execute(
            "UPDATE batch_items SET status = 'pending', updated_at = ? "
            "WHERE batch_id = ? AND status = 'submitting' AND updated_at < ? AND ? = 0",
            (utc_now(), batch["id"], stale_before.isoformat(timespec="seconds"), _refresh_only),
        )
        connection.commit()

        submitted = 0
        artifact_warnings: list[dict[str, Any]] = []
        asset_downloads: list[dict[str, Any]] = []
        refresh_errors: list[dict[str, Any]] = []
        pending = connection.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? AND status = 'pending' "
            "AND EXISTS (SELECT 1 FROM batches WHERE id = batch_id "
            "AND cancel_requested = 0 AND status = 'active') "
            "AND ? = 0 "
            "ORDER BY sequence LIMIT ?",
            (batch["id"], _refresh_only, submissions_limit),
        ).fetchall()
        for item in pending:
            claim = connection.execute(
                "UPDATE batch_items SET status = 'submitting', error_json = NULL, updated_at = ? "
                "WHERE id = ? AND status = 'pending' "
                "AND EXISTS (SELECT 1 FROM batches WHERE id = batch_id "
                "AND cancel_requested = 0 AND status = 'active')",
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
                    operation=("video.generate" if _template(batch).get("task_type") == "video.generate" else str(batch["operation"])),
                    model=str(batch["model"]),
                    idempotency_key=str(item["idempotency_key"]),
                    request=body,
                    output_dir=str(batch["output_dir"]),
                    base_url=client.base_url,
                    local_id=local_task_id,
                    status="submitting",
                )
                connection.commit()
                if cancel_blocked_batch_intent(connection, str(item["idempotency_key"])):
                    connection.commit()
                    continue
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
                    raise AsxError("服务未返回任务 ID，无法确认提交结果；请勿重复生成。", code="invalid_task_response")
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
                    error=friendly_task_error(task.get("error"), status=status) if status in {"failed", "timeout", "canceled"} else None,
                )
                connection.commit()
                submitted += 1
            except AsxError as exc:
                canceled = cancel_blocked_batch_intent(connection, str(item["idempotency_key"]))
                if exc.submission_rejected:
                    rejected_intent = get_state_task(connection, f"batchitem_{int(item['id'])}")
                    if rejected_intent is not None and not rejected_intent.get("remote_task_id"):
                        update_state_task(connection, str(rejected_intent["local_id"]), status="failed", error=exc.payload().get("error"))
                _update_item(
                    connection,
                    int(item["id"]),
                    status=(
                        "canceled" if canceled
                        else "failed" if exc.submission_rejected or exc.http_status in {400, 404, 410, 422}
                        else "pending"
                    ),
                    error=exc.payload().get("error"),
                )
                connection.commit()
                log(f"批次 {batch['id']} 第 {item['sequence']} 项提交失败：{exc.message}")

        remote_items = connection.execute(
            "SELECT * FROM batch_items WHERE batch_id = ? AND status IN "
            "('submitted', 'queued', 'running', 'delayed', 'canceling', 'succeeded') "
            "ORDER BY sequence",
            (batch["id"],),
        ).fetchall()
        downloaded = 0
        for item in remote_items:
            if item["status"] == "succeeded" and _completed_item_files(item):
                continue
            remote_task_id: str | None = item["task_id"]
            if not isinstance(remote_task_id, str):
                continue
            state_task_row = get_state_task(connection, remote_task_id=remote_task_id)
            batch_local_task_id = str(state_task_row["local_id"]) if state_task_row else None
            try:
                task, request_id = client.task(remote_task_id)
                task.setdefault("task_type", _template(batch).get("task_type"))
            except AsxError as exc:
                refresh_errors.append({
                    "sequence": item["sequence"], "task_id": remote_task_id,
                    "error": exc.payload().get("error"),
                })
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
                refresh_errors.append({
                    "sequence": item["sequence"], "task_id": remote_task_id,
                    "error": {
                        "code": "invalid_task_status",
                        "message": "无法识别服务返回的任务进度，请稍后刷新原任务。",
                    },
                })
                _update_item(
                    connection,
                    int(item["id"]),
                    status=item["status"],
                    request_id=request_id,
                    error={
                        "code": "invalid_task_status",
                        "message": "无法识别服务返回的任务进度，请稍后刷新原任务。",
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
                    error=friendly_task_error(task.get("error"), status=status) if status in {"failed", "timeout", "canceled"} else None,
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
                files = _completed_item_files(item)
                if not files:
                    completed_files: list[str] = []
                    try:
                        files = download_assets(
                            client,
                            task,
                            batch["output_dir"],
                            prefix=f"{batch['id']}-{int(item['sequence']):06d}",
                            metrics=asset_downloads,
                            local_task_id=batch_local_task_id,
                            completed_files=completed_files,
                        )
                        downloaded += len(files)
                    except AsxError as exc:
                        downloaded += len(completed_files)
                        _update_item(
                            connection,
                            int(item["id"]),
                            status="succeeded",
                            request_id=request_id,
                            files=completed_files,
                            error=exc.payload().get("error"),
                        )
                        connection.commit()
                        continue
                if files and not batch_local_task_id:
                    artifact_warning = index_downloaded_artifacts(
                        task, files, operation=("video.generate" if _template(batch).get("task_type") == "video.generate" else batch["operation"])
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
                    friendly_task_error(task.get("error"), status=status)
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
        if refresh_errors:
            result["refresh_errors"] = refresh_errors
            result["next_action_args"] = ["batch", "status", batch["id"], "--refresh"]
            result.setdefault("warnings", []).append({
                "code": "batch_refresh_incomplete",
                "message": "部分任务未能查询到最新状态，当前进度可能不是最新；请再次刷新该批次，无需重新提交。",
            })
        if _include_results:
            _attach_batch_results(connection, get_batch(connection, batch["id"]), result)
        return {"ok": not bool(result.get("partial") or refresh_errors), "batch": result}
    finally:
        connection.close()


def refresh_batch(client: AsynxClient, batch_id: str | None) -> dict[str, Any]:
    """Refresh already submitted tasks and downloads without advancing the queue."""
    return poll_batch(client, batch_id, _refresh_only=True)


def set_batch_paused(batch_id: str | None, paused: bool) -> dict[str, Any]:
    connection = connect_db()
    try:
        batch = get_batch(connection, batch_id)
        if batch["status"] in {"completed", "canceled"}:
            raise AsxError("该批次已结束或已取消，无法暂停或继续；如需更多结果请新建批次。", code="batch_finished")
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
            "UPDATE batches SET cancel_requested = 1, updated_at = ? WHERE id = ?",
            (utc_now(), batch["id"]),
        )
        connection.execute(
            "UPDATE tasks SET status = 'canceled', updated_at = ? "
            "WHERE remote_task_id IS NULL AND idempotency_key IN "
            "(SELECT idempotency_key FROM batch_items WHERE batch_id = ? "
            "AND status IN ('pending', 'submitting'))",
            (utc_now(), batch["id"]),
        )
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
        _refresh_batch_status(connection, batch["id"])
        connection.commit()
        summary = batch_summary(connection, get_batch(connection, batch["id"]))
        payload: dict[str, Any] = {"ok": True, "batch": summary}
        if summary["status"] != "canceled":
            payload["cancel_requested"] = True
            payload["warning"] = "取消请求可能仍在处理中，已开始的任务可能继续执行并产生费用。"
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
            "查询间隔须为 1–300 秒，请调整 --interval。", code="invalid_poll_interval"
        )
    if max_wait < 0:
        raise AsxError("最长等待时间不能为负数；--max-wait 设为 0 表示不限时。", code="invalid_max_wait")
    started = time.monotonic()
    while True:
        result = poll_batch(client, batch_id, _include_results=False)
        batch = result["batch"]
        if result.get("paused"):
            return _finish_batch_result(result)
        if batch["status"] in {"completed", "canceled"} and not batch["pending_download"]:
            return _finish_batch_result(result)
        if max_wait and time.monotonic() - started >= max_wait:
            result["wait_timeout"] = True
            return _finish_batch_result(result)
        time.sleep(interval)


def execute_batch(
    client: AsynxClient | None, args: argparse.Namespace
) -> tuple[dict[str, Any], int]:
    action = args.batch_command
    if action == "list":
        return list_batches(), 0
    if action == "status":
        refresh_warnings: list[dict[str, Any]] = []
        refresh_errors: list[dict[str, Any]] = []
        if args.refresh:
            if client is None:
                raise AsxError("尚未配置 API Key，无法查询最新进度；请在终端运行 configure 完成配置。", code="missing_api_key")
            # Status reads may download existing results, but never create paid tasks.
            refreshed = poll_batch(client, args.batch_id, _refresh_only=True, _include_results=False)
            refresh_warnings = refreshed["batch"].get("warnings", [])
            refresh_errors = refreshed["batch"].get("refresh_errors", [])
        snapshot = batch_status(args.batch_id, include_items=args.items)
        if refresh_warnings:
            snapshot["batch"].setdefault("warnings", []).extend(refresh_warnings)
        if refresh_errors:
            snapshot["ok"] = False
            snapshot["batch"]["refresh_errors"] = refresh_errors
            snapshot["batch"]["next_action_args"] = [
                "batch", "status", snapshot["batch"]["id"], "--refresh",
            ]
        return snapshot, 4 if refresh_errors else 0
    if action in {"pause", "resume"}:
        return set_batch_paused(args.batch_id, paused=action == "pause"), 0
    if client is None:
        raise AsxError("尚未配置 API Key，无法执行该操作；请在终端运行 configure 完成配置。", code="missing_api_key")
    if action == "poll":
        result = poll_batch(client, args.batch_id, submissions_limit=args.limit)
        return result, 0 if result["ok"] else 4
    if action == "wait":
        result = wait_for_batch(
            client, args.batch_id, interval=args.interval, max_wait=args.max_wait
        )
        return result, 0 if result["ok"] else 4
    if action == "cancel":
        return cancel_batch(client, args.batch_id), 0
    if action == "create":
        duration = getattr(args, "duration", None)
        log("正在创建批次并提交任务……")
        references = args.reference if args.operation in {"generate", "video.generate"} else args.image
        if args.operation == "video.generate" and args.image:
            raise AsxError("视频参考图请通过 --reference 或 --first-frame 传入。", code="conflicting_batch_options")
        if args.operation != "video.generate" and any(
            getattr(args, field, None) is not None
            for field in ("duration", "resolution", "first_frame", "last_frame", "reference_video", "reference_audio")
        ):
            raise AsxError("图片批次不支持视频参数。", code="conflicting_batch_options")
        if args.operation == "edit" and not references:
            raise AsxError("编辑批次至少需要一张输入图片。", code="missing_edit_image")
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
            options=options_from_args(args),
            duration=duration if duration is not None else 5,
            resolution=getattr(args, "resolution", None) or "720p",
            first_frame=getattr(args, "first_frame", None), last_frame=getattr(args, "last_frame", None),
            reference_videos=getattr(args, "reference_video", None),
            reference_audios=getattr(args, "reference_audio", None),
        )
        for warning in created.get("warnings", []):
            log(f"提示：{warning['message']}")
        progressed = poll_batch(client, created["id"], submissions_limit=args.limit)
        progressed["created"] = True
        if created.get("warnings"):
            progressed["warnings"] = created["warnings"]
        return progressed, 0 if progressed["ok"] else 4
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
            options=options_from_args(args),
            duration=getattr(args, "duration", None), resolution=getattr(args, "resolution", None),
            first_frame=getattr(args, "first_frame", None), last_frame=getattr(args, "last_frame", None),
            reference_videos=getattr(args, "reference_video", None),
            reference_audios=getattr(args, "reference_audio", None),
        )
        for warning in result.get("warnings", []):
            log(f"提示：{warning['message']}")
        progressed = poll_batch(client, result["id"], submissions_limit=args.limit)
        progressed["added"] = args.total
        if result.get("warnings"):
            progressed["warnings"] = result["warnings"]
        return progressed, 0 if progressed["ok"] else 4
    raise AssertionError("unreachable")
