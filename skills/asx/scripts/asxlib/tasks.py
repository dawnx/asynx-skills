from __future__ import annotations

import argparse
import os
import random
import secrets
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .client import AsynxClient
from .constants import DEFAULT_OUTPUT_DIR, KNOWN_STATUSES, TERMINAL_STATUSES, UTC
from .errors import AsxError
from .images import build_task, image_mime, validate_idempotency_key
from .output import log


def parse_deadline(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def wait_for_terminal(
    client: AsynxClient,
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
        delay = min(delay * 1.5, 10.0)
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
    candidate = directory / f"{base_name}.{extension}"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{base_name}-{suffix}.{extension}"
        suffix += 1
    return candidate


def download_assets(
    client: AsynxClient,
    task: dict[str, Any],
    output_dir: str,
    *,
    prefix: str | None = None,
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
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("index"), int):
            raise AsxError(
                "Task contains an invalid Asset",
                code="invalid_task_asset",
                task_id=task_id,
            )
        index = asset["index"]
        data, response_type = client.asset(task_id, index)
        extension = _extension(response_type or asset.get("content_type"), data)
        path = _unique_output_path(directory, f"{prefix or task_id}-{index}", extension)
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.part")
        try:
            temporary.write_bytes(data)
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise AsxError(f"Cannot save generated image: {exc}", code="output_write_failed") from exc
        files.append(str(path))
    return files


def task_payload(
    task: dict[str, Any], request_id: str | None, *, files: list[str] | None = None
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
    }
    if files is not None:
        payload["files"] = files
    if status in TERMINAL_STATUSES and status != "succeeded":
        payload["ok"] = False
        payload["error"] = task.get("error") or {
            "code": f"task_{status}",
            "message": f"Task ended with status {status}",
        }
    return {key: value for key, value in payload.items() if value is not None}


def run_submission(
    client: AsynxClient, args: argparse.Namespace, task_type: str
) -> tuple[dict[str, Any], int]:
    references = args.reference if task_type == "image.generate" else args.image
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
    )
    idempotency_key = validate_idempotency_key(
        args.idempotency_key or f"asx-{secrets.token_hex(16)}"
    )
    log(f"提交 {task_type}，模型：{model}")
    log(f"幂等键：{idempotency_key}")
    try:
        task, request_id = client.submit(body, idempotency_key)
    except AsxError as exc:
        if exc.details is None:
            exc.details = {}
        if isinstance(exc.details, dict):
            exc.details.setdefault("idempotency_key", idempotency_key)
            if catalog_request_id:
                exc.details.setdefault("catalog_request_id", catalog_request_id)
        raise
    if args.detach:
        payload = task_payload(task, request_id)
        payload["idempotency_key"] = idempotency_key
        return payload, 0
    task, request_id = wait_for_terminal(client, task, request_id)
    if task.get("status") != "succeeded":
        return task_payload(task, request_id), 4
    files = download_assets(client, task, args.output_dir)
    return task_payload(task, request_id, files=files), 0


def models_payload(client: AsynxClient, operation: str) -> dict[str, Any]:
    models, request_id = client.models()
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
    task, request_id = client.task(task_id)
    task, request_id = wait_for_terminal(client, task, request_id)
    if task.get("status") != "succeeded":
        return task_payload(task, request_id), 4
    files = download_assets(client, task, output_dir)
    return task_payload(task, request_id, files=files), 0
