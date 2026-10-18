from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import state_path
from .constants import UTC
from .errors import AsxError


def init_schema(connection: sqlite3.Connection) -> None:
    if connection.row_factory is None:
        connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            path TEXT NOT NULL,
            prompt TEXT,
            model TEXT,
            operation TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(task_id, path)
        );
        CREATE INDEX IF NOT EXISTS ix_artifacts_created_at
            ON artifacts(created_at DESC, id DESC);
        CREATE INDEX IF NOT EXISTS ix_artifacts_task_id
            ON artifacts(task_id, position);
        """
    )


def connect_db() -> sqlite3.Connection:
    path = state_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    init_schema(connection)
    path.chmod(0o600)
    return connection


def record_artifacts(
    connection: sqlite3.Connection,
    task_id: str,
    files: list[str],
    *,
    prompt: str | None = None,
    model: str | None = None,
    operation: str | None = None,
    created_at: str | None = None,
) -> list[dict[str, Any]]:
    if not task_id.strip():
        raise AsxError("Artifact Task ID is empty", code="invalid_artifact")
    timestamp = created_at or datetime.now(UTC).isoformat(timespec="seconds")
    records: list[dict[str, Any]] = []
    for position, value in enumerate(files):
        path = str(Path(value).expanduser().absolute())
        connection.execute(
            "INSERT INTO artifacts "
            "(task_id, position, path, prompt, model, operation, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(task_id, path) DO UPDATE SET "
            "position = excluded.position, prompt = COALESCE(excluded.prompt, artifacts.prompt), "
            "model = COALESCE(excluded.model, artifacts.model), "
            "operation = COALESCE(excluded.operation, artifacts.operation)",
            (task_id, position, path, prompt, model, operation, timestamp),
        )
        records.append(
            {
                "task_id": task_id,
                "position": position,
                "path": path,
                "name": Path(path).name,
                "prompt": prompt,
                "model": model,
                "operation": operation,
                "created_at": timestamp,
                "missing": not Path(path).is_file(),
            }
        )
    return records


def _row_payload(row: sqlite3.Row) -> dict[str, Any]:
    path = str(row["path"])
    return {
        "task_id": str(row["task_id"]),
        "position": int(row["position"]),
        "path": path,
        "name": Path(path).name,
        "prompt": row["prompt"],
        "model": row["model"],
        "operation": row["operation"],
        "created_at": row["created_at"],
        "missing": not Path(path).is_file(),
    }


def list_artifacts(
    connection: sqlite3.Connection,
    *,
    query: str | None = None,
    task_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    parameters: list[Any] = []
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        clauses.append(
            "(task_id LIKE ? OR path LIKE ? OR COALESCE(prompt, '') LIKE ? "
            "OR COALESCE(model, '') LIKE ?)"
        )
        parameters.extend([pattern] * 4)
    if task_id and task_id.strip():
        clauses.append("task_id = ?")
        parameters.append(task_id.strip())
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    bounded_limit = max(1, min(limit, 1000))
    rows = connection.execute(
        "SELECT task_id, position, path, prompt, model, operation, created_at "
        f"FROM artifacts{where} ORDER BY created_at DESC, id DESC LIMIT ?",
        (*parameters, bounded_limit),
    ).fetchall()
    return [_row_payload(row) for row in rows]


def artifact_paths(connection: sqlite3.Connection, task_id: str) -> list[str]:
    rows = list_artifacts(connection, task_id=task_id, limit=1000)
    if not rows:
        raise AsxError(
            f"No locally indexed artifacts for Task {task_id}",
            code="artifact_not_found",
            task_id=task_id,
        )
    missing = [str(row["path"]) for row in rows if row["missing"]]
    if missing:
        raise AsxError(
            f"Indexed artifact file is missing: {missing[0]}",
            code="artifact_file_missing",
            task_id=task_id,
            details={"missing": missing},
        )
    return [str(row["path"]) for row in sorted(rows, key=lambda item: item["position"])]


def recent_payload(
    query: str | None = None, limit: int = 20, *, latest: bool = False
) -> dict[str, Any]:
    from .state import connect_db as connect_ledger_db

    connection = connect_ledger_db()
    try:
        clauses: list[str] = []
        params: list[Any] = []
        if query and query.strip():
            pattern = f"%{query.strip()}%"
            clauses.append(
            "(a.artifact_id LIKE ? OR t.remote_task_id LIKE ? OR a.path LIKE ? "
                "OR t.request_json LIKE ? OR t.model LIKE ?)"
            )
            params.extend([pattern] * 5)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = connection.execute(
            "SELECT a.*, t.remote_task_id, t.request_json, t.model, t.operation, t.updated_at AS task_updated_at "
            "FROM assets AS a JOIN tasks AS t ON t.local_id = a.local_task_id"
            f"{where} ORDER BY a.updated_at DESC, a.remote_index LIMIT ?",
            (*params, 1 if latest else max(1, min(int(limit), 1000))),
        ).fetchall()
        items = []
        for row in rows:
            path = str(row["path"])
            try:
                request = json.loads(row["request_json"])
            except (TypeError, json.JSONDecodeError):
                request = {}
            prompt = request.get("input", {}).get("prompt") if isinstance(request, dict) else None
            items.append(
                {
                    "artifact_id": row["artifact_id"],
                    "local_id": row["local_task_id"],
                    "task_id": row["remote_task_id"],
                    "position": row["remote_index"],
                    "path": path,
                    "name": Path(path).name,
                    "prompt": prompt,
                    "model": row["model"],
                    "operation": row["operation"],
                    "created_at": row["created_at"],
                    "missing": not Path(path).is_file(),
                }
            )
        return {"ok": True, "schema_version": 1, "artifacts": items}
    finally:
        connection.close()
