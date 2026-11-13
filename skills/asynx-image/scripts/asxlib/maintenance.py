"""Pruning of the local ledger and of the images staged for upload.

Unfinished records are never touched: recovery settles them from what is stored.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .constants import UTC
from .errors import AsxError

_TASK_DONE = "('succeeded','failed','timeout','canceled','orphaned')"
_BATCH_DONE = "('completed','canceled')"
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
    """Drop staged images nothing can send any more and, on request, old finished records.

    Result files on disk stay.
    """
    if older_than_days is not None and (
        isinstance(older_than_days, bool)
        or not isinstance(older_than_days, int)
        or older_than_days < 1
    ):
        raise AsxError("清理天数必须是不小于 1 的整数。", code="invalid_prune_age")
    from . import state, uploads
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
        # After the deletions: finished records never keep a staged image alive.
        cleaned = uploads.clean(
            connection, min_age_seconds=uploads.UNREFERENCED_GRACE_SECONDS, dry_run=dry_run
        )
        if not dry_run:
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
            "deleted": deleted,
            "uploads": {**cleaned, "cache_dir": str(uploads.uploads_dir())},
            "kept_files": True,
            "bytes_before": bytes_before,
            "bytes_after": _stored_bytes(path),
            "state_path": path,
        }
    finally:
        connection.close()


__all__ = ["prune_state"]
