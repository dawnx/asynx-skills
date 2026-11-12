from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .media_results import MODEL_FILE_EXTENSIONS

# The prompt is matched on its own: request_json also embeds reference images as
# base64 Data URLs, and matching the whole document turns random base64 into hits.
_PROMPT_SQL = "json_extract(t.request_json, '$.input.prompt')"
_PROMPT_FALLBACK_SQL = "asx_request_prompt(t.request_json)"


def _request_prompt(request_json: str | None) -> str | None:
    try:
        request = json.loads(request_json or "")
    except (TypeError, ValueError):
        return None
    request_input = request.get("input") if isinstance(request, dict) else None
    prompt = request_input.get("prompt") if isinstance(request_input, dict) else None
    return prompt if isinstance(prompt, str) else None


def _path_tail(path: str | None) -> str | None:
    """The file name and its folder: what a person names. A query must not match
    every result just because a parent directory (say "Application Support") contains it."""
    return "/".join(Path(path).parts[-2:]) if isinstance(path, str) else None


def _recent_rows(
    connection: sqlite3.Connection,
    query: str | None,
    limit: int,
    modality: str | None,
    prompt_sql: str,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[Any] = []
    if modality:
        # Not every 3D result has a model/* type: FBX and archives are application/*.
        others = sorted(name for name in MODEL_FILE_EXTENSIONS if not name.startswith("model/")) if modality == "model" else []
        clauses.append("(a.mime LIKE ?" + " OR a.mime = ?" * len(others) + ")")
        params.extend([f"{modality}/%", *others])
    if query and query.strip():
        pattern = f"%{query.strip()}%"
        clauses.append(
            "(a.artifact_id LIKE ? OR t.remote_task_id LIKE ? OR asx_path_tail(a.path) LIKE ? "
            f"OR {prompt_sql} LIKE ? OR t.model LIKE ?)"
        )
        params.extend([pattern] * 5)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return connection.execute(
        "SELECT a.*, t.remote_task_id, t.request_json, t.model, t.operation, t.updated_at AS task_updated_at "
        "FROM assets AS a JOIN tasks AS t ON t.local_id = a.local_task_id"
        f"{where} ORDER BY a.updated_at DESC, a.remote_index LIMIT ?",
        (*params, limit),
    ).fetchall()


def recent_payload(
    query: str | None = None, limit: int = 20, *, latest: bool = False,
    modality: str | None = None,
) -> dict[str, Any]:
    from .state import connect_db as connect_ledger_db

    connection = connect_ledger_db()
    try:
        bounded = 1 if latest else max(1, min(int(limit), 1000))
        connection.create_function("asx_path_tail", 1, _path_tail)
        try:
            rows = _recent_rows(connection, query, bounded, modality, _PROMPT_SQL)
        except sqlite3.OperationalError:
            # SQLite built without JSON functions (or a row holding malformed JSON):
            # read the prompt in Python while SQLite keeps the same matching,
            # ordering and limit.
            connection.create_function("asx_request_prompt", 1, _request_prompt)
            rows = _recent_rows(connection, query, bounded, modality, _PROMPT_FALLBACK_SQL)
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
                    "task_type": request.get("task_type") if isinstance(request, dict) else None,
                    "kind": "model" if row["mime"] in MODEL_FILE_EXTENSIONS
                    else str(row["mime"] or "").split("/", 1)[0] or None,
                    "content_type": row["mime"],
                    "created_at": row["created_at"],
                    "missing": not Path(path).is_file(),
                }
            )
        return {"ok": True, "schema_version": 1, "artifacts": items}
    finally:
        connection.close()
