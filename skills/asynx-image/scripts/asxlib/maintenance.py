"""Ledger slimming and pruning.

Finished records keep their request bodies for history and search, but nothing
replays them any more, so inline reference images are replaced by a short
fingerprint.  Unfinished records are never touched: recovery resubmits them
from the stored body.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .constants import UTC
from .errors import AsxError

OMITTED_PREFIX = "asx-omitted:"

_BASE64_MARK = ";base64,"
_TASK_DONE = "('succeeded','failed','timeout','canceled','orphaned')"
_ITEM_DONE = "('succeeded','failed','timeout','canceled')"
_BATCH_DONE = "('completed','canceled')"
# (table, key column, JSON column, statuses in which the body is never replayed)
_SLIM_TARGETS = (
    ("tasks", "local_id", "request_json", _TASK_DONE),
    ("batch_items", "id", "body_json", _ITEM_DONE),
    ("batches", "id", "template_json", _BATCH_DONE),
)
_OLD_BATCHES = f"status IN {_BATCH_DONE} AND updated_at < :cutoff"
_OLD_BATCH_ITEMS = f"batch_id IN (SELECT id FROM batches WHERE {_OLD_BATCHES})"
# Finished tasks of a pruned batch leave with it; other finished tasks leave by
# age unless a batch that is still open may need them.
_OLD_TASKS = (
    f"status IN {_TASK_DONE} AND ("
    f"idempotency_key IN (SELECT idempotency_key FROM batch_items WHERE {_OLD_BATCH_ITEMS}) "
    "OR (updated_at < :cutoff AND idempotency_key NOT IN ("
    "SELECT i.idempotency_key FROM batch_items AS i JOIN batches AS b ON b.id = i.batch_id "
    f"WHERE b.status NOT IN {_BATCH_DONE})))"
)


def _omit(data_url: str) -> str:
    header, _, encoded = data_url.partition(_BASE64_MARK)
    mime = header[len("data:"):].split(";", 1)[0].strip() or "application/octet-stream"
    try:
        content = base64.b64decode(encoded, validate=True)
        size = len(content)
    except ValueError:
        content = encoded.encode("utf-8", "surrogatepass")
        size = len(encoded) * 3 // 4
    return f"{OMITTED_PREFIX}{mime};bytes={size};sha256={hashlib.sha256(content).hexdigest()[:16]}"


def slim_payload(value: Any) -> Any:
    """Return a deep copy with every inline Data URL replaced by its fingerprint."""
    if isinstance(value, str):
        return _omit(value) if value.startswith("data:") and _BASE64_MARK in value else value
    if isinstance(value, dict):
        return {key: slim_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [slim_payload(item) for item in value]
    return value


def slim_json(text: str | None) -> str | None:
    """Slim a serialized JSON value; ``None`` means there was nothing to change."""
    if not text or _BASE64_MARK not in text:
        return None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return None
    slimmed = slim_payload(value)
    if slimmed == value:
        return None
    # Must stay identical to the ledger's own serialization: stored request
    # bodies are compared as strings when an idempotency key is reused.
    return json.dumps(slimmed, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _compact(connection: sqlite3.Connection, *, write: bool) -> dict[str, int]:
    result = {"tasks": 0, "batch_items": 0, "batches": 0, "bytes_saved": 0}
    for table, key, column, statuses in _SLIM_TARGETS:
        if not _has_table(connection, table):
            continue
        identifiers = [
            row[0]
            for row in connection.execute(
                f"SELECT {key} FROM {table} "
                f"WHERE status IN {statuses} AND {column} LIKE '%;base64,%'"
            )
        ]
        # One body at a time: a single request can hold tens of megabytes.
        for identifier in identifiers:
            text = connection.execute(
                f"SELECT {column} FROM {table} WHERE {key} = ?", (identifier,)
            ).fetchone()[0]
            slimmed = slim_json(text)
            if slimmed is None:
                continue
            if write:
                connection.execute(
                    f"UPDATE {table} SET {column} = ? WHERE {key} = ?", (slimmed, identifier)
                )
            result[table] += 1
            result["bytes_saved"] += len(text.encode("utf-8")) - len(slimmed.encode("utf-8"))
    return result


def compact_state(connection: sqlite3.Connection) -> dict[str, int]:
    """Slim finished tasks, batch items and batch templates in place, without committing."""
    return _compact(connection, write=True)


def _stored_bytes(path: str) -> int:
    total = 0
    for candidate in (path, f"{path}-wal"):
        try:
            total += os.path.getsize(candidate)
        except OSError:
            pass
    return total


def _count(connection: sqlite3.Connection, table: str, where: str, cutoff: str) -> int:
    return int(
        connection.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {where}", {"cutoff": cutoff}
        ).fetchone()[0]
    )


def prune_state(*, older_than_days: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Slim the ledger and optionally drop old finished records; files on disk stay."""
    if older_than_days is not None and (
        isinstance(older_than_days, bool)
        or not isinstance(older_than_days, int)
        or older_than_days < 1
    ):
        raise AsxError("清理天数必须是不小于 1 的整数。", code="invalid_prune_age")
    from . import state
    from .config import state_path

    path = str(state_path().expanduser())
    connection = state.connect_db()
    try:
        state.ensure_batch_schema(connection)
        connection.commit()
        # Has no effect inside a transaction, so set it before the first write.
        connection.execute("PRAGMA foreign_keys = ON")
        bytes_before = _stored_bytes(path)
        if not dry_run:
            # Take the write lock first so counts and deletions see one snapshot.
            connection.execute("BEGIN IMMEDIATE")
        compacted = _compact(connection, write=not dry_run)
        deleted = {"tasks": 0, "batches": 0, "batch_items": 0}
        if older_than_days is not None:
            cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).isoformat(
                timespec="seconds"
            )
            deleted = {
                "tasks": _count(connection, "tasks", _OLD_TASKS, cutoff),
                "batches": _count(connection, "batches", _OLD_BATCHES, cutoff),
                "batch_items": _count(connection, "batch_items", _OLD_BATCH_ITEMS, cutoff),
            }
            if not dry_run:
                # Tasks first: their selection reads the batch rows removed next.
                # Assets, events and batch items follow through ON DELETE CASCADE.
                connection.execute(f"DELETE FROM tasks WHERE {_OLD_TASKS}", {"cutoff": cutoff})
                connection.execute(f"DELETE FROM batches WHERE {_OLD_BATCHES}", {"cutoff": cutoff})
        if not dry_run:
            connection.commit()
            try:
                connection.execute("VACUUM")
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error:
                # Another process is using the ledger; the space is reused later.
                pass
        return {
            "ok": True,
            "schema_version": 1,
            "dry_run": dry_run,
            "compacted": compacted,
            "deleted": deleted,
            "kept_files": True,
            "bytes_before": bytes_before,
            "bytes_after": _stored_bytes(path),
            "state_path": path,
        }
    finally:
        connection.close()


__all__ = ["OMITTED_PREFIX", "compact_state", "prune_state", "slim_json", "slim_payload"]
