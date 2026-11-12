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
from .config import default_output_root, initial_poll_delay_seconds, max_wait_seconds
from .constants import (
    DEFAULT_OUTPUT_DIR,
    KNOWN_STATUSES,
    MAX_TASK_POLL_DELAY_SECONDS,
    TERMINAL_STATUSES,
    UTC,
)
from .errors import AsxError, friendly_task_error
from .image_options import options_from_args
from .image_results import requested_aspect_ratio
from .images import (
    build_task,
    cache_models,
    image_mime,
    task_input_warnings,
    validate_idempotency_key,
)
from .media_results import (
    attach_media_details,
    incomplete_model_file,
    media_file_info,
    model_content_type,
)
from .models3d import (
    MODEL_OPERATION_NAMES,
    MODEL_SOURCE_TASK_TYPES,
    RIG_CHECK_TASK_TYPE,
    body_from_model_args,
    body_from_model_followup_args,
    rig_check_answer,
)
from .output import log
from .state import (
    append_event,
    bind_remote_task,
    cancel_blocked_batch_intent,
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
from .videos import body_from_video_args

_SAFE_PATH_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_STATUS_LABELS = {
    "queued": "排队中", "running": "生成中", "delayed": "等待重试",
    "canceling": "正在取消", "succeeded": "已完成", "failed": "生成失败",
    "timeout": "已超时", "canceled": "已取消",
}
_OPERATION_LABELS = {
    "image.generate": "图片生成", "image.edit": "图片编辑", "video.generate": "视频生成",
    **{task_type: f" 3D 模型{name}" for task_type, name in MODEL_OPERATION_NAMES.items()},
}
# Video and 3D work takes minutes, so their tasks are polled less often.
_LONG_TASK_PREFIXES = ("video.", "model.")


def _output_root() -> str:
    return default_output_root(DEFAULT_OUTPUT_DIR)


def _task_folder(local_id: str, prompt: Any) -> str:
    """Name a task's default folder so a person can find it: date, subject, short ID."""
    words = re.sub(r"[^\w]+", "-", prompt if isinstance(prompt, str) else "").strip("-_")[:32].strip("-_")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return "-".join(part for part in (stamp, words, local_id[-6:]) if part)


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
    initial_delay: float = MAX_TASK_POLL_DELAY_SECONDS,
    poll_interval: float | None = None,
    max_wait: float = 0,
) -> tuple[dict[str, Any], str | None]:
    """Poll until the task ends, or until ``max_wait`` seconds pass (0: no bound).

    A bounded wait returns the still-running task; nothing is canceled.
    """
    task_id = task.get("id")
    if not isinstance(task_id, str):
        raise AsxError("服务未返回任务 ID，暂时无法确认提交结果。请保留本地任务记录，通过 task recover 确认，避免重复生成。", code="invalid_task_response")
    original_task_type = task.get("task_type")
    deadline = parse_deadline(task.get("deadline_at"))
    client_deadline = (
        deadline + timedelta(seconds=30)
        if deadline
        else datetime.now(UTC) + timedelta(minutes=65 if str(task.get("task_type", "")).startswith(_LONG_TASK_PREFIXES) else 20)
    )
    interval = poll_interval or (10.0 if str(task.get("task_type", "")).startswith(_LONG_TASK_PREFIXES) else MAX_TASK_POLL_DELAY_SECONDS)
    delay = initial_delay
    last_status: str | None = None
    stop_at = time.monotonic() + max_wait if max_wait > 0 else None
    while True:
        status = task.get("status")
        if not isinstance(status, str) or status not in KNOWN_STATUSES:
            raise AsxError(
                "服务返回了无法识别的任务状态，请稍后查询原任务，避免重复生成。",
                code="invalid_task_status",
                task_id=task_id,
            )
        if status != last_status:
            log(f"任务 {task_id}：{_STATUS_LABELS[status]}")
            last_status = status
        if status in TERMINAL_STATUSES:
            return task, request_id
        if datetime.now(UTC) >= client_deadline:
            raise AsxError(
                "本次等待已超时，但任务未被取消。请稍后查询或继续等待原任务，无需重新生成。",
                code="client_wait_timeout",
                exit_code=4,
                task_id=task_id,
                request_id=request_id,
                details={"next_action_args": ["wait", task_id]},
            )
        if stop_at is not None:
            remaining = stop_at - time.monotonic()
            if remaining <= 0:
                return task, request_id
            delay = min(delay, remaining)
        try:
            sleep(delay)
            delay = interval + random.uniform(0.0, interval * 0.15)
            task, request_id = client.task(task_id)
            if original_task_type:
                task.setdefault("task_type", original_task_type)
        except KeyboardInterrupt:
            # The host stopped this command, not the task: report which one to resume.
            raise AsxError(
                "等待已中断，任务未取消，仍在服务端继续。请继续等待原任务，无需重新生成。",
                code="interrupted",
                exit_code=130,
                task_id=task_id,
                request_id=request_id,
                details={"next_action_args": ["wait", task_id]},
            ) from None
        except AsxError as exc:
            exc.task_id = task_id
            if exc.details is None:
                exc.details = {}
            if isinstance(exc.details, dict):
                exc.details.setdefault("next_action_args", ["wait", task_id])
            raise


def _extension(content_type: str | None, data: bytes) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    if normalized in {"image/jpeg", "image/jpg"}:
        return "jpg"
    if normalized == "image/webp":
        return "webp"
    if normalized == "image/png":
        return "png"
    return {"image/jpeg": "jpg", "image/webp": "webp", "image/png": "png", "image/gif": "gif", "image/svg+xml": "svg"}[
        image_mime(data)
    ]


def _unique_output_path(directory: Path, base_name: str, extension: str) -> Path:
    safe_name = _SAFE_PATH_COMPONENT.sub("_", base_name).strip("._") or "asset"
    return directory / f"{safe_name}.{extension}"


def _is_model_file(path: Path) -> bool:
    try:
        return bool(media_file_info(path)["kind"] == "model")
    except (OSError, AsxError):
        return False


def _invalid_model_asset(task_id: str) -> AsxError:
    return AsxError(
        "下载到的文件不是完整有效的 3D 模型文件，未作为结果交付。请稍后通过 wait 重新下载原任务，"
        "无需重新生成或重新提交；若持续出现，请保留任务 ID 并联系服务支持。",
        code="invalid_model_asset", task_id=task_id,
    )


def _verify_model_download(task_id: str, asset: dict[str, Any], info: dict[str, Any]) -> None:
    """The service validated the file it stored; this catches a wrong or cut download of it."""
    declared = model_content_type(asset.get("content_type"))
    if info["kind"] != "model" or (declared is not None and info["content_type"] != declared):
        raise _invalid_model_asset(task_id)
    declared_size = asset.get("size_bytes")
    if type(declared_size) is int and declared_size != info["size_bytes"]:
        error = incomplete_model_file("下载到的大小与服务记录的文件大小不符。")
        error.task_id = task_id
        raise error


def _model_source_task_id(identifier: str, task_type: str) -> str:
    """Resolve ``--from-task`` to the server task ID a 3D follow-up names as its source.

    A local record that already rules the source out stops the command before
    anything is submitted; without a record the service decides.
    """
    connection = connect_ledger_db()
    try:
        row = get_ledger_task(connection, local_task_id=identifier) or get_ledger_task(
            connection, remote_task_id=identifier
        )
    finally:
        connection.close()
    if row is None:
        if identifier.startswith("local_"):
            raise AsxError(f"本机未找到任务 {identifier}，请检查任务 ID，或通过 task list 查看现有任务。", code="task_not_found")
        return identifier
    operation = MODEL_OPERATION_NAMES[task_type]
    accepted = MODEL_SOURCE_TASK_TYPES[task_type]
    request = row.get("request")
    source_type = request.get("task_type") if isinstance(request, dict) else None
    if source_type not in accepted:
        raise AsxError(
            f"任务 {identifier} 不能作为{operation}的来源：需要 "
            + "、".join(f"3D 模型{MODEL_OPERATION_NAMES[name]}" for name in accepted) + "任务的结果。"
            + ("格式转换后的模型不能再作为来源。" if source_type == "model.convert" else ""),
            code="model_source_incompatible",
            details={"source_task_type": source_type, "accepted_task_types": list(accepted)},
        )
    status = str(row.get("status"))
    remote_id = row.get("remote_task_id")
    if not isinstance(remote_id, str):
        raise AsxError(
            f"来源任务 {identifier} 的提交尚未确认，不能用于{operation}。请先通过 task recover 确认原提交。",
            code="model_source_not_ready",
            details={"source_status": status, "next_action_args": ["task", "recover", identifier]},
        )
    if status != "succeeded":
        ended = status in TERMINAL_STATUSES
        raise AsxError(
            f"来源任务 {identifier} 未成功完成（本机记录的状态：{_STATUS_LABELS.get(status, status)}），不能用于{operation}。"
            + ("请改用成功完成的任务。" if ended else "请先等待该任务完成，再提交本操作。"),
            code="model_source_not_ready",
            details={"source_status": status} if ended else {
                "source_status": status, "next_action_args": ["wait", identifier],
            },
        )
    return remote_id


def _download_previews(
    client: AsynxClient,
    task: dict[str, Any],
    output_dir: str,
    files: list[str],
    *,
    prefix: str | None,
    metrics: list[dict[str, Any]] | None,
    local_task_id: str | None,
    warnings: list[dict[str, Any]] | None,
) -> None:
    """Fetch a 3D task's optional preview images; a missing or failed one never fails delivery."""
    result = task["result"]
    auxiliary = result.get("auxiliary_assets")
    model_indices = {asset["index"] for asset in result["assets"]}
    for asset in auxiliary if isinstance(auxiliary, list) else []:
        if (
            not isinstance(asset, dict) or asset.get("kind") != "image"
            or not isinstance(asset.get("index"), int) or asset["index"] in model_indices
        ):
            continue
        try:
            # Saved exactly like an image task's asset: verified by content, cached by index.
            download_assets(
                client, {"id": task["id"], "result": {"assets": [asset]}}, output_dir,
                prefix=prefix, metrics=metrics, local_task_id=local_task_id, completed_files=files,
            )
        except (AsxError, OSError, sqlite3.Error):
            if warnings is not None:
                warnings.append({
                    "code": "preview_unavailable",
                    "message": "3D 模型已保存，但预览图未能下载，不影响模型文件；需要预览图时可通过 wait 补下载。",
                })


def download_assets(
    client: AsynxClient,
    task: dict[str, Any],
    output_dir: str,
    *,
    prefix: str | None = None,
    metrics: list[dict[str, Any]] | None = None,
    local_task_id: str | None = None,
    completed_files: list[str] | None = None,
    warnings: list[dict[str, Any]] | None = None,
) -> list[str]:
    task_id = task.get("id")
    result = task.get("result")
    assets = result.get("assets") if isinstance(result, dict) else None
    if not isinstance(task_id, str) or not isinstance(assets, list) or not assets:
        raise AsxError(
            "任务显示已完成，但没有可下载的结果文件。请保留任务 ID 并联系服务支持。",
            code="missing_task_assets",
            task_id=task_id if isinstance(task_id, str) else None,
        )
    directory = Path(output_dir).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    files: list[str] = completed_files if completed_files is not None else []
    is_video_task = str(task.get("task_type", "")).startswith("video.")
    is_model_task = str(task.get("task_type", "")).startswith("model.")
    has_video = False
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
                "服务返回的结果文件信息不完整，暂时无法下载。请查询原任务，或联系服务支持。",
                code="invalid_task_asset",
                task_id=task_id,
            )
        index = asset["index"]
        asset_metrics: dict[str, Any] = {}
        download_url = asset.get("download_url") if isinstance(asset.get("download_url"), str) else None
        cached = cached_assets.get(index)
        cached_path = cached.get("path") if cached else None
        # A damaged local model is replaced from the original task, never delivered as-is.
        if isinstance(cached_path, str) and cached_path and Path(cached_path).is_file() and (
            not is_model_task or _is_model_file(Path(cached_path))
        ):
            if is_video_task:
                has_video = has_video or media_file_info(Path(cached_path))["kind"] == "video"
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
        temporary = _unique_output_path(directory, f"{task_id}-{index}-{secrets.token_hex(4)}", "part")
        save_started = time.perf_counter()
        try:
            if is_model_task:
                declared_type = model_content_type(asset.get("content_type"))
                client.asset_to_file(
                    task_id, index, temporary, download_url, metrics=asset_metrics,
                    model_content_type=declared_type,
                )
                file_info = media_file_info(temporary, model_content_type_hint=declared_type)
                _verify_model_download(task_id, asset, file_info)
            elif is_video_task:
                client.asset_to_file(task_id, index, temporary, download_url, metrics=asset_metrics)
                file_info = media_file_info(temporary)
            if is_video_task or is_model_task:
                actual_mime = str(file_info["content_type"])
                has_video = has_video or file_info["kind"] == "video"
                extension = str(file_info["extension"])
                size_bytes = int(file_info["size_bytes"])
                checksum = str(file_info["sha256"])
            else:
                data, _response_type = client.asset(task_id, index, download_url, metrics=asset_metrics)
                actual_mime = image_mime(data)
                extension = _extension(actual_mime, data)
                size_bytes = len(data)
                checksum = hashlib.sha256(data).hexdigest()
                temporary.write_bytes(data)
            path = (
                Path(cached_path)
                if isinstance(cached_path, str) and cached_path
                else _unique_output_path(directory, f"{prefix or task_id}-{index}", extension)
            )
            path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(temporary, path)
        except AsxError as exc:
            if is_model_task and exc.code == "unsupported_media_format":
                raise _invalid_model_asset(task_id) from exc
            raise
        except OSError as exc:
            raise AsxError("结果文件保存失败，请检查输出目录的写入权限和剩余空间，然后继续下载原任务。", code="output_write_failed") from exc
        finally:
            temporary.unlink(missing_ok=True)
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
                    bytes=size_bytes,
                    sha256=checksum,
                    state="available",
                )
                connection.commit()
            finally:
                connection.close()
        files.append(str(path))
    if is_video_task and not has_video:
        raise AsxError(
            "任务返回了附件，但没有可交付的 MP4 视频。已下载的文件仍保留，请检查原任务。",
            code="missing_video_asset", task_id=task_id,
        )
    if is_model_task:
        _download_previews(
            client, task, output_dir, files, prefix=prefix, metrics=metrics,
            local_task_id=local_task_id, warnings=warnings,
        )
    return files


def index_downloaded_artifacts(
    task: dict[str, Any], files: list[str], *, operation: str | None = None
) -> dict[str, Any] | None:
    remote_id = task.get("id")
    if not isinstance(remote_id, str):
        return {"code": "artifact_index_failed", "message": "结果缺少任务 ID，无法写入本地历史记录。请保留已下载的文件。"}
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
                output_dir=str(Path(files[0]).parent) if files else _output_root(),
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
        assets = task.get("result", {}).get("assets", [])
        for position, path in enumerate(files):
            index = assets[position].get("index", position) if position < len(assets) else position
            info = media_file_info(Path(path))
            upsert_asset(
                connection,
                local_id,
                index,
                path=path,
                mime=info["content_type"],
                bytes=info["size_bytes"],
                sha256=info["sha256"],
                state="available",
            )
        connection.commit()
    except (OSError, sqlite3.Error, AsxError):
        return {
            "code": "artifact_index_failed",
            "message": "结果文件已保存，但未能写入本地历史记录。请保留文件路径，并检查本地目录的写入权限和剩余空间。",
        }
    finally:
        connection.close()
    return None


def _task_identifier_row(connection: sqlite3.Connection, identifier: str) -> dict[str, Any]:
    row = get_ledger_task(connection, local_task_id=identifier)
    if row is None:
        row = get_ledger_task(connection, remote_task_id=identifier)
    if row is None:
        raise AsxError(f"本机未找到任务 {identifier}，请检查任务 ID，或通过 task list 查看现有任务。", code="task_not_found")
    return row


def _local_task_payload(row: dict[str, Any]) -> dict[str, Any]:
    remote = row.get("remote")
    request = row.get("request")
    task_type = request.get("task_type") if isinstance(request, dict) else None
    payload: dict[str, Any] = {
        "ok": row.get("status") not in {"failed", "timeout", "canceled", "orphaned"},
        "schema_version": 1,
        "local_id": row.get("local_id"),
        "task_id": row.get("remote_task_id"),
        "status": row.get("status"),
        "model": row.get("model"),
        "operation": row.get("operation"),
        "task_type": task_type,
        "modality": task_type.split(".", 1)[0] if isinstance(task_type, str) else "image",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "last_polled": row.get("last_polled"),
        "billing": row.get("billing"),
        "error": row.get("error"),
    }
    if isinstance(remote, dict):
        if payload.get("error") and payload["error"] == remote.get("error"):
            payload["error"] = friendly_task_error(payload["error"], status=row.get("status"))
        for key in ("result_quality", "result", "deadline_at", "started_at", "finished_at"):
            if remote.get(key) is not None:
                payload[key] = remote[key]
        payload["rig_check"] = rig_check_answer(
            {"task_type": task_type, "status": row.get("status"), "result": remote.get("result")}
        )
    _task_followup(payload)
    return {key: value for key, value in payload.items() if value is not None}


def _task_followup(payload: dict[str, Any]) -> None:
    identifier = payload.get("local_id") or payload.get("task_id")
    status = payload.get("status")
    if not identifier or status in {"failed", "timeout", "canceled", "orphaned"}:
        return
    if payload.get("rig_check") is not None:
        # The answer is the whole result: there is nothing left to wait for or download.
        return
    if status in {"intent", "submitting", "unknown"} and not payload.get("task_id"):
        payload["next_action"] = "task recover"
        payload["next_action_args"] = ["task", "recover", identifier]
    else:
        payload["next_action"] = "wait"
        payload["next_action_args"] = ["wait", identifier]


def _persist_task_intent(
    client: AsynxClient,
    body: dict[str, Any],
    *,
    operation: str,
    model: str,
    idempotency_key: str,
    output_dir: str,
) -> tuple[dict[str, Any], bool]:
    """Record the submission first; also report whether it replays an unanswered attempt."""
    local_id = f"local_{secrets.token_hex(12)}"
    task_input = body.get("input")
    prompt = task_input.get("prompt") if isinstance(task_input, dict) else None
    resolved_output_dir = (
        str((Path(_output_root()).expanduser() / _task_folder(local_id, prompt)).resolve())
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
        replay = unanswered_attempt(row) and str(row["local_id"]) != local_id
        if not row.get("remote_task_id") and row.get("status") != "submitting":
            row = update_task(connection, str(row["local_id"]), status="submitting")
        append_event(connection, str(row["local_id"]), "submit_started")
        connection.commit()
        return row, replay
    finally:
        connection.close()


def unanswered_attempt(row: dict[str, Any] | None) -> bool:
    """Whether this record was sent before without a definite answer.

    If so the task may already exist, and a refusal of the replay cannot settle it.
    """
    if row is None or row.get("remote_task_id"):
        return False
    return str(row.get("status")) in {"submitting", "unknown"}


def _bind_submission(
    client: AsynxClient,
    local_id: str,
    task: dict[str, Any],
    request_id: str | None,
) -> dict[str, Any]:
    remote_id = task.get("id")
    if not isinstance(remote_id, str):
        raise AsxError("服务未返回任务 ID，暂时无法确认提交结果。请通过 task recover 确认，避免重复生成。", code="invalid_task_response")
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
        changes = {"error": exc.payload().get("error")}
        if exc.submission_rejected:
            changes["status"] = "failed"
        update_task(connection, local_id, **changes)
        append_event(connection, local_id, "submit_error", exc.payload().get("error"))
        connection.commit()
    finally:
        connection.close()


def _remote_task_for_row(client: AsynxClient, row: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    remote_id = row.get("remote_task_id")
    if not isinstance(remote_id, str):
        raise AsxError(
            "该任务的提交尚未确认。请通过 task recover 指定该任务 ID 确认原提交，避免重新生成。",
            code="task_not_submitted",
            task_id=str(row.get("local_id")),
            details={"next_action_args": ["task", "recover", str(row["local_id"])]},
        )
    task, request_id = client.task(remote_id)
    request = row.get("request")
    if isinstance(request, dict) and request.get("task_type"):
        task.setdefault("task_type", request["task_type"])
    return task, request_id


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
        raise AsxError("服务返回了无法识别的任务状态，请稍后查询原任务，避免重复生成。", code="invalid_task_status")
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
            error=friendly_task_error(task.get("error"), status=status) if status in {"failed", "timeout", "canceled"} else None,
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
    connection = connect_ledger_db()
    try:
        current_row = get_ledger_task(connection, local_task_id=local_id) or row
    finally:
        connection.close()
    if status == "succeeded" and download:
        target_dir = output_dir or str(row.get("output_dir") or _output_root())
        delivered, code = _download_task_result(
            client, task, request_id, target_dir, local_id=local_id,
            expected_ratio=requested_aspect_ratio(row.get("request")),
        )
        for key in ("operation", "created_at", "updated_at", "last_polled"):
            if current_row.get(key) is not None:
                delivered[key] = current_row[key]
        return delivered, code
    payload = _local_task_payload(current_row)
    payload["request_id"] = request_id
    return payload, 0 if status not in {"failed", "timeout", "canceled"} else 4


def refresh_task(client: AsynxClient, identifier: str) -> tuple[dict[str, Any], int]:
    """Query one local or remote task, downloading results without submitting work."""
    connection = connect_ledger_db()
    try:
        row = get_ledger_task(connection, local_task_id=identifier) or get_ledger_task(
            connection, remote_task_id=identifier
        )
    finally:
        connection.close()
    if row is not None:
        if not row.get("remote_task_id"):
            return _local_task_payload(row), 0
        return sync_local_task(client, identifier)
    if identifier.startswith("local_"):
        raise AsxError(f"本机未找到任务：{identifier}", code="task_not_found")
    task, request_id = client.task(identifier)
    if task.get("status") == "succeeded":
        return _download_task_result(client, task, request_id, _output_root())
    payload = task_payload(task, request_id)
    return payload, 0 if payload["ok"] else 4


def recover_tasks(
    client: AsynxClient, identifier: str | None = None, *, limit: int = 50,
    max_age_hours: float = 24,
) -> dict[str, Any]:
    """Confirm unbound submissions by replaying them with their original key.

    A replay creates the task when the first attempt never reached the service,
    so without an explicit ID only recent records are replayed.
    """
    if max_age_hours <= 0:
        raise AsxError("恢复时限须为正数（小时）。", code="invalid_recover_age")
    replay_after = datetime.now(UTC) - timedelta(hours=max_age_hours)
    connection = connect_ledger_db()
    try:
        if identifier:
            rows = [_task_identifier_row(connection, identifier)]
        else:
            # Look at unfinished records only: newer finished tasks must not push an
            # unconfirmed submission out of the window that is examined.
            rows = [
                row for row in list_ledger_tasks(connection, limit=1000)
                if str(row.get("status")) not in TERMINAL_STATUSES
            ][:limit]
    finally:
        connection.close()
    results: list[dict[str, Any]] = []
    for row in rows:
        status = str(row.get("status"))
        if status in TERMINAL_STATUSES:
            if not identifier:
                continue
            if status != "succeeded":
                results.append(_local_task_payload(row))
                continue
        replay = unanswered_attempt(row)
        try:
            if not row.get("remote_task_id"):
                created = parse_deadline(row.get("created_at"))
                if not identifier and (created is None or created < replay_after):
                    stale = _local_task_payload(row)
                    stale.update(
                        skipped="stale",
                        warnings=[{
                            "code": "stale_unconfirmed_submission",
                            "message": (
                                f"该提交已超过 {max_age_hours:g} 小时仍未确认，未自动重新提交。"
                                "仍需要时请指定该任务 ID 恢复（可能创建并计费），不需要时可取消该记录。"
                            ),
                        }],
                    )
                    results.append(stale)
                    continue
                connection = connect_ledger_db()
                try:
                    blocked = cancel_blocked_batch_intent(connection, str(row["idempotency_key"]))
                    paused_batch = None
                    if connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'batch_items'"
                    ).fetchone():
                        paused_batch = connection.execute(
                            "SELECT b.id FROM batch_items i JOIN batches b ON b.id = i.batch_id "
                            "WHERE i.idempotency_key = ? AND b.status = 'paused'",
                            (row["idempotency_key"],),
                        ).fetchone()
                    connection.commit()
                finally:
                    connection.close()
                if blocked:
                    if identifier:
                        results.append(_local_task_payload({**row, "status": "canceled"}))
                    continue
                if paused_batch is not None:
                    paused = _local_task_payload(row)
                    paused.update(
                        paused=True,
                        next_action="batch status",
                        next_action_args=["batch", "status", paused_batch["id"], "--refresh"],
                        warnings=[{
                            "code": "batch_paused",
                            "message": "该任务所在的批次已暂停，尚未恢复提交。如需继续生成，请先恢复该批次。",
                        }],
                    )
                    results.append(paused)
                    continue
                body = row.get("request")
                if not isinstance(body, dict):
                    raise AsxError("本地提交记录不完整，暂时无法恢复。请保留记录并联系服务支持确认原任务，避免重复生成。", code="invalid_task_state")
                task, request_id = client.submit(body, str(row["idempotency_key"]))
                row = _bind_submission(client, str(row["local_id"]), task, request_id)
            synced, _code = sync_local_task(client, str(row["local_id"]))
            results.append(synced)
        except AsxError as exc:
            exc.replayed = exc.replayed or replay
            if exc.submission_rejected and not row.get("remote_task_id"):
                _record_submit_error(str(row["local_id"]), exc)
                row = {**row, "status": "failed"}
            results.append({**_local_task_payload(row), "ok": False, "error": exc.payload()["error"]})
    return {"ok": all(item.get("ok", False) for item in results), "schema_version": 1, "tasks": results}


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
            error=friendly_task_error(task.get("error"), status=local_status) if local_status in TERMINAL_STATUSES and local_status != "succeeded" else None,
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
        payload["warning"] = "取消请求仍在处理中，已开始的任务可能继续执行并计费。请稍后查询最终状态。"
    return payload, 0


def task_payload(
    task: dict[str, Any],
    request_id: str | None,
    *,
    files: list[str] | None = None,
    local_id: str | None = None,
    expected_ratio: str | None = None,
) -> dict[str, Any]:
    status = task.get("status")
    if not isinstance(status, str) or status not in KNOWN_STATUSES:
        raise AsxError("服务返回了无法识别的任务状态，请稍后查询原任务，避免重复生成。", code="invalid_task_status", task_id=task.get("id"))
    payload: dict[str, Any] = {
        "ok": status == "succeeded" or status not in TERMINAL_STATUSES,
        "task_id": task.get("id"),
        "status": status,
        "task_type": task.get("task_type"),
        "model": task.get("model"),
        "result_quality": task.get("result_quality"),
        "billing": task.get("billing"),
        "request_id": request_id,
        "schema_version": 1,
        "local_id": local_id,
        "rig_check": rig_check_answer(task),
    }
    if files is not None:
        payload["files"] = files
        attach_media_details(payload, files, expected_ratio=expected_ratio or requested_aspect_ratio(task), task=task)
    if status in TERMINAL_STATUSES and status != "succeeded":
        payload["ok"] = False
        payload["error"] = friendly_task_error(task.get("error"), status=status)
    if files is None:
        _task_followup(payload)
    return {key: value for key, value in payload.items() if value is not None}


def _download_task_result(
    client: AsynxClient,
    task: dict[str, Any],
    request_id: str | None,
    output_dir: str,
    *,
    local_id: str | None = None,
    expected_ratio: str | None = None,
    metrics: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], int]:
    if task.get("task_type") == RIG_CHECK_TASK_TYPE:
        # A check answers with data and delivers no files, so an empty asset list is not a failure.
        payload = task_payload(task, request_id, local_id=local_id)
        payload["files"] = []
        if "rig_check" in payload:
            return payload, 0
        payload["ok"] = False
        payload["error"] = {
            "code": "missing_rig_check_result", "phase": "result",
            "message": "绑定检查已完成，但服务未返回检查结果。请稍后通过 wait 重新查询原任务，无需重新提交。",
        }
        return payload, 4
    files: list[str] = []
    download_error: dict[str, Any] | None = None
    preview_warnings: list[dict[str, Any]] = []
    try:
        download_assets(
            client, task, output_dir, local_task_id=local_id,
            metrics=metrics, completed_files=files, warnings=preview_warnings,
        )
    except KeyboardInterrupt:
        identifier = local_id or task.get("id")
        raise AsxError(
            "下载被中断，生成已完成，已保存的文件仍可使用。请继续下载原任务，无需重新生成。",
            code="interrupted",
            exit_code=130,
            task_id=task.get("id") if isinstance(task.get("id"), str) else None,
            details={"next_action_args": ["wait", identifier]} if identifier else None,
        ) from None
    except AsxError as exc:
        download_error = exc.payload()["error"]
    except (OSError, sqlite3.Error):
        download_error = {"code": "output_write_failed", "message": "结果保存未完成，请检查目录写入权限和剩余空间，再继续下载原任务。"}
    payload = task_payload(
        task, request_id, files=files, local_id=local_id, expected_ratio=expected_ratio,
    )
    payload["download_status"] = "failed" if download_error else "downloaded"
    if preview_warnings:
        payload.setdefault("warnings", []).extend(preview_warnings)
    if not local_id and files:
        warning = index_downloaded_artifacts(task, files)
        if warning:
            payload.setdefault("warnings", []).append(warning)
    if download_error:
        payload["ok"] = False
        payload["error"] = {**download_error, "phase": "download"}
        payload.setdefault("warnings", []).append({
            "code": "download_incomplete",
            "message": "生成已完成，但下载未完成；已保存的文件仍可使用。请继续下载原任务，避免重新生成。",
        })
        _task_followup(payload)
        return payload, 4
    return payload, 0


def _update_local_snapshot(
    local_id: str,
    task: dict[str, Any],
    request_id: str | None,
) -> None:
    status = task.get("status")
    if not isinstance(status, str):
        raise AsxError("服务返回了无法识别的任务状态，请稍后查询原任务，避免重复生成。", code="invalid_task_status")
    connection = connect_ledger_db()
    try:
        current = get_ledger_task(connection, local_task_id=local_id)
        if current is None:
            raise AsxError(f"本机未找到任务 {local_id}，请检查任务 ID，或通过 task list 查看现有任务。", code="task_not_found")
        previous = str(current.get("status"))
        update_task(
            connection,
            local_id,
            status=status,
            remote=task,
            error=friendly_task_error(task.get("error"), status=status) if status in {"failed", "timeout", "canceled"} else None,
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


def quote_payload(client: AsynxClient, body: dict[str, Any], *, tasks: int = 1) -> dict[str, Any]:
    """Ask the service what a prepared request would cost; nothing is submitted or saved."""
    from decimal import Decimal, InvalidOperation

    data, request_id = client.quote(body)
    try:
        amount = Decimal(str(data["charge_amount"]))
    except (KeyError, InvalidOperation, ValueError) as exc:
        raise AsxError("服务返回的报价不完整，请稍后重试；若持续出现，请联系服务支持。", code="invalid_quote_response") from exc
    quote: dict[str, Any] = {
        "model": data.get("model") or body.get("model"),
        "currency": data.get("currency") or "USD",
        "amount_per_task": str(amount),
        "tasks": tasks,
        "total_amount": str(amount * tasks),
        "pricing": data.get("pricing"),
        "price_source": data.get("price_source"),
    }
    payload: dict[str, Any] = {
        "ok": True,
        "schema_version": 1,
        "submitted": False,
        "quote": {key: value for key, value in quote.items() if value is not None},
        "message": "仅报价，未提交任务，也未产生费用。实际扣费以任务完成后的计费结果为准。",
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


def wait_limit(args: argparse.Namespace) -> float:
    explicit = getattr(args, "max_wait", None)
    if explicit is None:
        return max_wait_seconds()
    if explicit < 0:
        raise AsxError("等待上限不能为负数；0 表示等到任务截止。", code="invalid_max_wait")
    return float(explicit)


def _still_running(payload: dict[str, Any]) -> dict[str, Any]:
    payload["wait_timeout"] = True
    payload.setdefault("warnings", []).append({
        "code": "wait_limit_reached",
        "message": "已达到本次等待上限，任务仍在服务端继续，未被取消。请继续等待原任务，无需重新生成。",
    })
    return payload


def run_submission(
    client: AsynxClient, args: argparse.Namespace, task_type: str
) -> tuple[dict[str, Any], int]:
    started_at = time.perf_counter()
    initial_delay = initial_poll_delay_seconds() if not args.detach else 0.0
    wait_bound = wait_limit(args)
    log(f"正在准备{_OPERATION_LABELS[task_type]}……")
    references = args.reference if task_type in {"image.generate", "video.generate"} else getattr(args, "image", None)
    from_task = getattr(args, "from_task", None)
    if task_type == "image.edit" and from_task:
        if references:
            raise AsxError(
                "请通过 --image 指定图片，或通过 --from-task 使用已有任务的结果，两者不能同时使用。", code="conflicting_edit_inputs"
            )
        connection = connect_ledger_db()
        try:
            references = ledger_task_paths(connection, from_task)
            if any(media_file_info(Path(path))["content_type"] in {"image/gif", "image/svg+xml"} for path in references):
                raise AsxError("GIF/SVG 结果不能直接作为编辑输入；请明确选择静态图片，不会自动取首帧或栅格化。", code="unsupported_edit_source")
        finally:
            connection.close()
    if task_type in MODEL_SOURCE_TASK_TYPES:
        body, model, catalog_request_id = body_from_model_followup_args(
            client, args, task_type, _model_source_task_id(str(from_task), task_type)
        )
    elif task_type == "model.generate":
        if from_task:
            if args.image:
                raise AsxError("--from-task 与 --image 不能同时使用。", code="conflicting_model_inputs")
            connection = connect_ledger_db()
            try:
                paths = ledger_task_paths(connection, from_task)
            finally:
                connection.close()
            if len(paths) != 1 or media_file_info(Path(paths[0]))["content_type"] not in {
                "image/png", "image/jpeg", "image/webp",
            }:
                raise AsxError(
                    "请选择一张 PNG、JPEG 或 WebP 图片作为参考图，通过 --image 传入路径；"
                    "不能直接使用多张结果、动画、视频或 3D 模型。",
                    code="ambiguous_model_source",
                )
            args = argparse.Namespace(**vars(args))
            args.image = paths[0]
        body, model, catalog_request_id = body_from_model_args(client, args)
    elif task_type == "video.generate":
        if from_task:
            if args.first_frame:
                raise AsxError("--from-task 与 --first-frame 不能同时使用。", code="conflicting_video_inputs")
            connection = connect_ledger_db()
            try:
                paths = ledger_task_paths(connection, from_task)
            finally:
                connection.close()
            if len(paths) != 1 or media_file_info(Path(paths[0]))["kind"] != "image":
                raise AsxError(
                    "请选择一张图片作为首帧，通过 --first-frame 传入路径；不能直接使用多张结果或视频。",
                    code="ambiguous_video_source",
                )
            args = argparse.Namespace(**vars(args))
            args.first_frame = paths[0]
        body, model, catalog_request_id = body_from_video_args(client, args)
    else:
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
            options=options_from_args(args),
        )
    prepared_at = time.perf_counter()
    input_warnings = task_input_warnings(body)
    for warning in input_warnings:
        log(f"提示：{warning['message']}")
    if getattr(args, "quote", False):
        quoted = quote_payload(client, body)
        if input_warnings:
            quoted["warnings"] = input_warnings
        return quoted, 0
    idempotency_key = validate_idempotency_key(
        args.idempotency_key or f"asx-{secrets.token_hex(16)}"
    )
    local_row, replay = _persist_task_intent(
        client,
        body,
        operation=task_type.removeprefix("image."),
        model=model,
        idempotency_key=idempotency_key,
        output_dir=args.output_dir,
    )
    local_id = str(local_row["local_id"])
    output_dir = str(local_row.get("output_dir") or args.output_dir)
    log(f"正在提交{_OPERATION_LABELS[task_type]}任务……")
    submitted_at = time.perf_counter()
    try:
        if local_row.get("remote_task_id"):
            task, request_id = client.task(str(local_row["remote_task_id"]))
        else:
            task, request_id = client.submit(body, idempotency_key)
            task.setdefault("task_type", task_type)
            _bind_submission(client, local_id, task, request_id)
        task.setdefault("task_type", task_type)
    except KeyboardInterrupt:
        # Stopped mid-request: only a replay under the same key can tell whether it arrived.
        raise AsxError(
            "提交被中断，尚未确认服务是否已受理。请恢复该记录确认原提交，不要直接重新生成。",
            code="interrupted",
            exit_code=130,
            details={"local_id": local_id, "next_action_args": ["task", "recover", local_id]},
        ) from None
    except AsxError as exc:
        exc.replayed = exc.replayed or replay
        _record_submit_error(local_id, exc)
        if exc.details is None:
            exc.details = {}
        if isinstance(exc.details, dict):
            exc.details.setdefault("local_id", local_id)
            if not exc.submission_rejected:
                exc.details.setdefault("next_action_args", ["task", "recover", local_id])
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
        return payload, 0 if payload["ok"] else 4
    # Printed at acceptance so a host that stops this command still shows what to resume.
    log(f"任务已受理：{task.get('id')}（本地记录 {local_id}）。若本命令被中断，请用 wait {local_id} 继续，不要重新生成。")
    task, request_id = wait_for_terminal(
        client, task, request_id,
        initial_delay=MAX_TASK_POLL_DELAY_SECONDS if local_row.get("remote_task_id") else initial_delay,
        poll_interval=10.0 if task_type.startswith(_LONG_TASK_PREFIXES) else None,
        max_wait=wait_bound,
    )
    _update_local_snapshot(local_id, task, request_id)
    finished_at = time.perf_counter()
    timings["wait_seconds"] = round(finished_at - accepted_at, 3)
    if task.get("status") != "succeeded":
        payload = task_payload(task, request_id, local_id=local_id)
        payload["timings"] = timings
        if input_warnings:
            payload["warnings"] = input_warnings
        if task.get("status") not in TERMINAL_STATUSES:
            return _still_running(payload), 0
        return payload, 4
    asset_metrics: list[dict[str, Any]] = []
    payload, code = _download_task_result(
        client, task, request_id, output_dir, metrics=asset_metrics, local_id=local_id,
        expected_ratio=requested_aspect_ratio(body),
    )
    timings["download_seconds"] = round(time.perf_counter() - finished_at, 3)
    timings["asset_downloads"] = asset_metrics
    payload["timings"] = timings
    if input_warnings:
        payload.setdefault("warnings", []).extend(input_warnings)
    return payload, code


def models_payload(client: AsynxClient, operation: str, *, modality: str = "image") -> dict[str, Any]:
    models, request_id = client.models()
    cache_models(client.base_url, models, scope=client.cache_scope)
    task_type = None if operation == "all" else f"{modality}.{operation}"
    items = []
    for model in models:
        if modality != "all" and model.get("modality", "image") != modality:
            continue
        if model.get("modality") == "video" and model.get("name") != "MiniMax-H3":
            continue
        if modality == "all" and operation != "all" and not any(
            str(value).endswith(f".{operation}") for value in model.get("task_types", [])
        ):
            continue
        if modality != "all" and task_type and task_type not in model.get("task_types", []):
            continue
        items.append(
            {
                key: model.get(key)
                for key in (
                    "name",
                    "modality",
                    "task_types",
                    "capabilities",
                    "pricing",
                    "official_discount_percent",
                    "image_parameters",
                    "price_source",
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
        raise AsxError("暂时无法读取任务历史，请稍后重试；若持续出现，请联系服务支持。", code="invalid_api_response")
    readable_items = [
        {**item, "error": friendly_task_error(item["error"], status=item.get("status"))}
        if isinstance(item, dict) and item.get("error") else item
        for item in items
    ]
    payload: dict[str, Any] = {"ok": True, "tasks": readable_items}
    next_cursor = data.get("next_cursor")
    if next_cursor:
        payload["next_cursor"] = next_cursor
    if request_id:
        payload["request_id"] = request_id
    return payload


def wait_and_download(
    client: AsynxClient, task_id: str, output_dir: str = DEFAULT_OUTPUT_DIR, *, max_wait: float = 0,
) -> tuple[dict[str, Any], int]:
    ledger_connection = connect_ledger_db()
    try:
        local_row = get_ledger_task(ledger_connection, local_task_id=task_id)
        if local_row is None:
            local_row = get_ledger_task(ledger_connection, remote_task_id=task_id)
    finally:
        ledger_connection.close()
    if local_row is not None:
        task, request_id = _remote_task_for_row(client, local_row)
        task, request_id = wait_for_terminal(client, task, request_id, max_wait=max_wait)
        local_id = str(local_row["local_id"])
        _update_local_snapshot(local_id, task, request_id)
        if task.get("status") not in TERMINAL_STATUSES:
            return _still_running(task_payload(task, request_id, local_id=local_id)), 0
        if task.get("status") != "succeeded":
            return task_payload(task, request_id, local_id=local_id), 4
        target_dir = (
            str(local_row.get("output_dir") or _output_root())
            if output_dir == DEFAULT_OUTPUT_DIR
            else output_dir
        )
        download_started = time.perf_counter()
        asset_metrics: list[dict[str, Any]] = []
        payload, code = _download_task_result(
            client, task, request_id, target_dir, metrics=asset_metrics, local_id=local_id,
            expected_ratio=requested_aspect_ratio(local_row.get("request")),
        )
        payload["timings"] = {
            "download_seconds": round(time.perf_counter() - download_started, 3),
            "asset_downloads": asset_metrics,
        }
        return payload, code
    task, request_id = client.task(task_id)
    task, request_id = wait_for_terminal(client, task, request_id, max_wait=max_wait)
    if task.get("status") not in TERMINAL_STATUSES:
        return _still_running(task_payload(task, request_id)), 0
    if task.get("status") != "succeeded":
        return task_payload(task, request_id), 4
    download_started = time.perf_counter()
    remote_asset_metrics: list[dict[str, Any]] = []
    payload, code = _download_task_result(
        client, task, request_id,
        _output_root() if output_dir == DEFAULT_OUTPUT_DIR else output_dir,
        metrics=remote_asset_metrics,
    )
    payload["timings"] = {
        "download_seconds": round(time.perf_counter() - download_started, 3),
        "asset_downloads": remote_asset_metrics,
    }
    return payload, code
