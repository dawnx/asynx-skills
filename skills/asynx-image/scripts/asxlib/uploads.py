"""Local files a request refers to: kept once on disk, uploaded once per account.

A prepared image, or a local 3D model, is written to a content-addressed file
and the request holds a short token naming it. The token depends only on the
content (and, for a model, the format its extension names), so the same request
compares equal whenever it is built, and the ledger never stores file bytes.
Just before a request is sent, each token is exchanged for the ID of a file
uploaded to the service; that ID is remembered until shortly before it expires.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import state
from .config import cache_path
from .errors import AsxError
from .media_results import image_mime
from .output import log

if TYPE_CHECKING:
    from .client import AsynxClient

TOKEN_PREFIX = "asx-upload:"
# How the service refuses a request naming a file it does not have (client contract).
MISSING_FILE_CODE = "file_not_found"
# The service types a model upload by its extension, so a model token carries it.
MODEL_UPLOAD_TYPES = {
    "glb": "model/gltf-binary", "fbx": "application/vnd.autodesk.fbx", "obj": "model/obj", "stl": "model/stl",
}
_TOKEN = re.compile(r"asx-upload:([0-9a-f]{64})(?:\.(glb|fbx|obj|stl))?")
# A recorded file is reused only while it outlives the submission comfortably.
REUSE_MARGIN_SECONDS = 10 * 60
# A blob is staged moments before the record that refers to it is written.
UNREFERENCED_GRACE_SECONDS = 60 * 60
# Blobs nothing refers to are dropped on the way, without waiting for ``prune``.
STALE_BLOB_SECONDS = 30 * 24 * 60 * 60
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
# Records whose request may still be sent, as (table, JSON column, condition).
_PENDING_REQUESTS = (
    ("tasks", "request_json", "status NOT IN ('succeeded','failed','timeout','canceled','orphaned')"),
    ("batch_items", "body_json", "status NOT IN ('succeeded','failed','timeout','canceled')"),
    ("batches", "template_json", "status NOT IN ('completed','canceled')"),
)


def uploads_dir() -> Path:
    return cache_path().expanduser().parent / "uploads"


def _missing(model: bool = False) -> AsxError:
    if model:
        return AsxError(
            "本地 3D 模型文件在本机的暂存副本已不存在，这次请求无法继续。请用原模型文件重新创建任务。",
            code="model_file_cache_missing",
            next_step="change_request",
        )
    return AsxError(
        "参考图在本机的暂存副本已不存在，这次请求无法继续。请用原图片文件重新创建任务。",
        code="reference_image_cache_missing",
        next_step="change_request",
    )


def stage(data: bytes, model_format: str | None = None) -> str:
    """Keep prepared bytes on disk and return the token a request holds for them.

    ``model_format`` marks a local 3D model of that format (a ``MODEL_UPLOAD_TYPES``
    key); without it the bytes are a prepared still image.
    """
    if model_format is not None and model_format not in MODEL_UPLOAD_TYPES:
        raise ValueError(f"unsupported model upload format: {model_format}")
    digest = hashlib.sha256(data).hexdigest()
    directory = uploads_dir()
    path = directory / digest
    temporary: str | None = None
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            present = path.stat().st_size == len(data)
            if present:
                # In use again: keep clean-up away from it.
                os.utime(path)
        except OSError:
            # Absent, or removed by a clean-up running at this moment.
            present = False
        if not present:
            with tempfile.NamedTemporaryFile(
                dir=directory, prefix="stage-", suffix=".tmp", delete=False
            ) as handle:
                temporary = handle.name
                handle.write(data)
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            temporary = None
    except OSError as exc:
        label = "3D 模型文件" if model_format else "参考图"
        raise AsxError(
            f"{label}无法写入本机缓存，请检查该目录的写入权限与磁盘空间：{directory}",
            code="model_file_cache_write_failed" if model_format else "reference_image_cache_write_failed",
        ) from exc
    finally:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    return TOKEN_PREFIX + digest + (f".{model_format}" if model_format else "")


def is_token(value: Any) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def read(token: str) -> bytes:
    """The bytes a token stands for; fails when the local copy is gone or damaged."""
    match = _TOKEN.fullmatch(token)
    if match is None:
        raise _missing()
    model = match[2] is not None
    try:
        data = (uploads_dir() / match[1]).read_bytes()
    except OSError as exc:
        raise _missing(model) from exc
    if hashlib.sha256(data).hexdigest() != match[1]:
        raise _missing(model)
    return data


def _keys(value: Any, found: list[str] | None = None) -> list[str]:
    """Distinct upload keys a request body refers to, in order of appearance.

    A key is the token without its prefix: the digest, plus the format for a model,
    since the same bytes uploaded as another format are another file to the service.
    """
    found = [] if found is None else found
    if isinstance(value, str):
        match = _TOKEN.fullmatch(value)
        if match is not None and match[0].removeprefix(TOKEN_PREFIX) not in found:
            found.append(match[0].removeprefix(TOKEN_PREFIX))
    elif isinstance(value, dict):
        for item in value.values():
            _keys(item, found)
    elif isinstance(value, list):
        for item in value:
            _keys(item, found)
    return found


def _replaced(value: Any, file_ids: dict[str, str]) -> Any:
    if isinstance(value, str):
        match = _TOKEN.fullmatch(value)
        return file_ids[match[0].removeprefix(TOKEN_PREFIX)] if match is not None else value
    if isinstance(value, dict):
        return {key: _replaced(item, file_ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_replaced(item, file_ids) for item in value]
    return copy.deepcopy(value)


def _size_text(size: int) -> str:
    return f"{size / 1024 / 1024:.2f} MiB" if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KiB"


def materialize(client: AsynxClient, body: dict[str, Any]) -> dict[str, Any]:
    """A copy of the body with every token replaced by a file ID valid for this account."""
    keys = _keys(body)
    if not keys:
        return copy.deepcopy(body)
    # A file belongs to the account that uploaded it: never reuse one elsewhere.
    owner = (str(client.base_url), str(client.cache_scope))
    file_ids: dict[str, str] = {}
    connection = state.connect_db()
    try:
        usable_after = int(time.time()) + REUSE_MARGIN_SECONDS
        for key in keys:
            row = connection.execute(
                "SELECT file_id FROM uploads "
                "WHERE sha256 = ? AND base_url = ? AND scope = ? AND expires_at > ?",
                (key, *owner, usable_after),
            ).fetchone()
            if row is not None:
                file_ids[key] = str(row[0])
        blobs = {key: read(TOKEN_PREFIX + key) for key in keys if key not in file_ids}
        if blobs:
            models = sum(1 for key in blobs if "." in key)
            counts = [
                text for count, text in (
                    (len(blobs) - models, f"{len(blobs) - models} 张本地图片"),
                    (models, f"{models} 个本地 3D 模型文件"),
                ) if count
            ]
            log(
                f"正在上传 {'和 '.join(counts)}"
                f"（共 {_size_text(sum(len(data) for data in blobs.values()))}）……"
            )
        for key, data in blobs.items():
            model_format = key.partition(".")[2]
            if model_format:
                uploaded, _ = client.upload_file(
                    data, MODEL_UPLOAD_TYPES[model_format], f"model.{model_format}", purpose="model"
                )
            else:
                content_type = image_mime(data)
                uploaded, _ = client.upload_file(
                    data, content_type, f"reference.{_EXTENSIONS.get(content_type, 'bin')}"
                )
            # The ``sha256`` column holds the key, which for a model ends in its format.
            connection.execute(
                "INSERT OR REPLACE INTO uploads "
                "(sha256, base_url, scope, file_id, expires_at, bytes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, *owner, uploaded["id"], uploaded["expires_at"], len(data), state.utc_now()),
            )
            connection.commit()
            file_ids[key] = str(uploaded["id"])
        if blobs:
            try:
                clean(connection, min_age_seconds=STALE_BLOB_SECONDS)
            except (OSError, sqlite3.Error):
                # Housekeeping only; ``prune`` does the same on request.
                connection.rollback()
    finally:
        connection.close()
    return _replaced(body, file_ids)


def forget(client: AsynxClient, body: dict[str, Any]) -> None:
    """Drop the files recorded for a body's images, so the next send uploads them again."""
    keys = _keys(body)
    if not keys:
        return
    connection = state.connect_db()
    try:
        connection.executemany(
            "DELETE FROM uploads WHERE sha256 = ? AND base_url = ? AND scope = ?",
            [(key, str(client.base_url), str(client.cache_scope)) for key in keys],
        )
        connection.commit()
    finally:
        connection.close()


def _for_submission(client: AsynxClient, body: dict[str, Any]) -> dict[str, Any]:
    try:
        return materialize(client, body)
    except AsxError as exc:
        # The submission itself was never sent, so it created nothing.
        exc.task_created = False
        exc.replayed = False
        raise


def submit(
    client: AsynxClient, body: dict[str, Any], idempotency_key: str, *, settled: bool = True
) -> tuple[dict[str, Any], str | None]:
    """Send a stored request, uploading its local images and models first.

    A file the service no longer has is uploaded again and the request sent once
    more under the same key, but only when the service said that no task was
    created and (``settled``) no earlier attempt may still be in flight.
    """
    try:
        return client.submit(_for_submission(client, body), idempotency_key)
    except AsxError as exc:
        if (
            exc.code != MISSING_FILE_CODE or not exc.submission_rejected
            or not settled or not _keys(body)
        ):
            raise
    forget(client, body)
    return client.submit(_for_submission(client, body), idempotency_key)


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _needed_digests(connection: sqlite3.Connection) -> set[str]:
    """Blobs an unfinished task, unfinished batch item or open batch refers to."""
    needed: set[str] = set()
    for table, column, condition in _PENDING_REQUESTS:
        if not _has_table(connection, table):
            continue
        for (text,) in connection.execute(
            f"SELECT {column} FROM {table} WHERE {condition} AND {column} LIKE '%{TOKEN_PREFIX}%'"
        ):
            needed.update(match[1] for match in _TOKEN.finditer(text))
    return needed


def clean(
    connection: sqlite3.Connection, *, min_age_seconds: float, dry_run: bool = False
) -> dict[str, int]:
    """Delete expired upload records and local blobs no sendable request needs.

    A blob is kept while an unfinished record refers to it, and for
    ``min_age_seconds`` after it was last staged.
    """
    now = time.time()
    expired = int(
        connection.execute(
            "SELECT COUNT(*) FROM uploads WHERE expires_at <= ?", (int(now),)
        ).fetchone()[0]
    )
    if not dry_run:
        connection.execute("DELETE FROM uploads WHERE expires_at <= ?", (int(now),))
        connection.commit()
    needed = _needed_digests(connection)
    result = {"blobs_deleted": 0, "blob_bytes_freed": 0, "expired_records_deleted": expired}
    try:
        entries = list(os.scandir(uploads_dir()))
    except OSError:
        return result
    for entry in entries:
        try:
            if not entry.is_file(follow_symlinks=False) or entry.name in needed:
                continue
            # Anything else here is a temporary file an interrupted write left behind.
            information = os.stat(entry.path, follow_symlinks=False)
            if now - information.st_mtime < min_age_seconds:
                continue
            if not dry_run:
                os.unlink(entry.path)
        except OSError:
            continue
        result["blobs_deleted"] += 1
        result["blob_bytes_freed"] += information.st_size
    return result


__all__ = [
    "MODEL_UPLOAD_TYPES",
    "TOKEN_PREFIX",
    "clean",
    "forget",
    "is_token",
    "materialize",
    "read",
    "stage",
    "submit",
    "uploads_dir",
]
