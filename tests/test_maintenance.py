from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "skills" / "asynx-image" / "scripts"))

from asxlib import maintenance, state, uploads
from asxlib.errors import AsxError

IMAGE = bytes(range(256)) * 8
OLD = "2020-01-01T00:00:00+00:00"


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def body(prompt: str, reference: str = "https://images.example/reference.png") -> dict[str, Any]:
    return {
        "task_type": "image.edit",
        "model": "test-model",
        "input": {"prompt": prompt, "reference_images": [reference]},
    }


class LedgerMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CACHE_PATH": str(self.root / "cache" / "models.json"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.connection = state.connect_db()
        self.addCleanup(self.connection.close)
        state.ensure_batch_schema(self.connection)

    def add_task(
        self, name: str, status: str, updated_at: str, *, key: str | None = None,
        reference: str = "https://images.example/reference.png",
    ) -> None:
        self.connection.execute(
            "INSERT INTO tasks (local_id, remote_task_id, operation, model, status, "
            "idempotency_key, request_json, output_dir, created_at, updated_at, base_url) "
            "VALUES (?, ?, 'edit', 'test-model', ?, ?, ?, ?, ?, ?, 'https://example.test')",
            (
                name, None if status == "submitting" else f"remote_{name}", status,
                key or f"key-{name}", dump(body(f"提示词 {name}", reference)), str(self.root), updated_at, updated_at,
            ),
        )

    def add_batch(
        self, name: str, status: str, updated_at: str, item_status: str,
        reference: str = "https://images.example/reference.png",
    ) -> None:
        self.connection.execute(
            "INSERT INTO batches (id, operation, name, status, model, output_dir, template_json, "
            "created_at, updated_at) VALUES (?, 'edit', ?, ?, 'test-model', ?, ?, ?, ?)",
            (name, name, status, str(self.root), dump(body(f"模板 {name}", reference)), updated_at, updated_at),
        )
        self.connection.execute(
            "INSERT INTO batch_items (batch_id, sequence, body_json, idempotency_key, status, updated_at) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (name, dump({"_asx_inherit_template": True}), f"key-{name}", item_status, updated_at),
        )

    def value(self, sql: str, *parameters: Any) -> Any:
        return self.connection.execute(sql, parameters).fetchone()[0]

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        return {
            table: [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("tasks", "assets", "task_events", "batches", "batch_items")
        }

    def staged(self, content: bytes) -> tuple[str, Path]:
        """Stage an image long enough ago that only a record can keep it."""
        token = uploads.stage(content)
        path = uploads.uploads_dir() / token.removeprefix(uploads.TOKEN_PREFIX)
        os.utime(path, (0, 0))
        return token, path

    def test_prune_keeps_staged_images_unfinished_records_still_send(self) -> None:
        # Deleting the local copy of a reference makes every unsent item of a batch unsendable.
        by_task, task_blob = self.staged(b"unfinished task")
        by_template, template_blob = self.staged(b"open batch, items inherit the template")
        by_finished, finished_blob = self.staged(b"finished task")
        _unused, orphan_blob = self.staged(b"never referenced")
        self.add_task("unfinished", "submitting", OLD, reference=by_task)
        self.add_task("finished", "succeeded", OLD, reference=by_finished)
        self.add_batch("open", "active", OLD, "pending", reference=by_template)
        self.connection.execute(
            "INSERT INTO uploads VALUES ('a', 'https://example.test', 's', 'file_old', 1, 3, ?), "
            "('b', 'https://example.test', 's', 'file_new', 99999999999, 3, ?)", (OLD, OLD),
        )
        self.connection.commit()
        removed_bytes = finished_blob.stat().st_size + orphan_blob.stat().st_size

        preview = maintenance.prune_state(dry_run=True)
        self.assertTrue(finished_blob.exists() and orphan_blob.exists())
        pruned = maintenance.prune_state()

        self.assertEqual(pruned["uploads"], {
            "blobs_deleted": 2, "blob_bytes_freed": removed_bytes, "expired_records_deleted": 1,
            "cache_dir": str(uploads.uploads_dir()),
        })
        self.assertEqual(preview["uploads"], pruned["uploads"])
        self.assertTrue(task_blob.exists() and template_blob.exists())
        self.assertFalse(finished_blob.exists() or orphan_blob.exists())
        self.assertEqual(
            [row[0] for row in self.connection.execute("SELECT file_id FROM uploads")], ["file_new"]
        )

    def test_prune_removes_only_old_finished_records_and_leaves_result_files(self) -> None:
        result_file = self.root / "result.png"
        result_file.write_bytes(IMAGE)
        self.add_task("old", "succeeded", OLD)
        self.add_task("recent", "succeeded", state.utc_now())
        self.add_task("unfinished", "submitting", OLD)
        self.add_task("open-item", "succeeded", OLD, key="key-open")
        self.add_task("done-item", "failed", OLD, key="key-done")
        self.add_batch("open", "paused", OLD, "succeeded")
        self.add_batch("done", "completed", OLD, "failed")
        state.upsert_asset(self.connection, "old", 0, path=str(result_file), mime="image/png")
        state.append_event(self.connection, "old", "status_changed", {"to": "succeeded"})
        self.connection.commit()
        before = self.snapshot()
        expected = {"tasks": 2, "batches": 1, "batch_items": 1}

        with self.assertRaises(AsxError) as rejected:
            maintenance.prune_state(older_than_days=0)
        self.assertEqual(rejected.exception.code, "invalid_prune_age")
        preview = maintenance.prune_state(older_than_days=30, dry_run=True)
        self.assertTrue(preview["dry_run"])
        self.assertEqual(preview["deleted"], expected)
        self.assertEqual(self.snapshot(), before)

        pruned = maintenance.prune_state(older_than_days=30)

        self.assertEqual(pruned["deleted"], expected)
        self.assertTrue(pruned["kept_files"])
        after = self.snapshot()
        self.assertEqual([row[0] for row in after["tasks"]], ["open-item", "recent", "unfinished"])
        self.assertEqual([row[0] for row in after["batches"]], ["open"])
        self.assertEqual(len(after["batch_items"]), 1)
        self.assertEqual(after["assets"] + after["task_events"], [])
        self.assertEqual(
            self.value("SELECT request_json FROM tasks WHERE local_id = 'unfinished'"),
            dump(body("提示词 unfinished")),
        )
        self.assertEqual(result_file.read_bytes(), IMAGE)


if __name__ == "__main__":
    unittest.main()
