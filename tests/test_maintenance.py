from __future__ import annotations

import base64
import hashlib
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

from asxlib import maintenance, state
from asxlib.errors import AsxError

IMAGE = bytes(range(256)) * 8
DATA_URL = "data:image/png;base64," + base64.b64encode(IMAGE).decode()
OMITTED = f"asx-omitted:image/png;bytes={len(IMAGE)};sha256={hashlib.sha256(IMAGE).hexdigest()[:16]}"
OLD = "2020-01-01T00:00:00+00:00"


def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def body(prompt: str) -> dict[str, Any]:
    return {
        "task_type": "image.edit",
        "model": "test-model",
        "input": {"prompt": prompt, "reference_images": [DATA_URL]},
    }


class LedgerMaintenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"ASYNX_STATE_PATH": str(self.root / "state.db")})
        environment.start()
        self.addCleanup(environment.stop)
        # Rows are written with plain SQL so they look like a ledger saved by an
        # older version, whatever the current write path does to new records.
        self.connection = state.connect_db()
        self.addCleanup(self.connection.close)
        state.ensure_batch_schema(self.connection)

    def add_task(self, name: str, status: str, updated_at: str, *, key: str | None = None) -> None:
        self.connection.execute(
            "INSERT INTO tasks (local_id, remote_task_id, operation, model, status, "
            "idempotency_key, request_json, output_dir, created_at, updated_at, base_url) "
            "VALUES (?, ?, 'edit', 'test-model', ?, ?, ?, ?, ?, ?, 'https://example.test')",
            (
                name, None if status == "submitting" else f"remote_{name}", status,
                key or f"key-{name}", dump(body(f"提示词 {name}")), str(self.root), updated_at, updated_at,
            ),
        )

    def add_batch(self, name: str, status: str, updated_at: str, item_status: str) -> None:
        self.connection.execute(
            "INSERT INTO batches (id, operation, name, status, model, output_dir, template_json, "
            "created_at, updated_at) VALUES (?, 'edit', ?, ?, 'test-model', ?, ?, ?, ?)",
            (name, name, status, str(self.root), dump(body(f"模板 {name}")), updated_at, updated_at),
        )
        self.connection.execute(
            "INSERT INTO batch_items (batch_id, sequence, body_json, idempotency_key, status, updated_at) "
            "VALUES (?, 1, ?, ?, ?, ?)",
            (name, dump(body(f"批次项 {name}")), f"key-{name}", item_status, updated_at),
        )

    def value(self, sql: str, *parameters: Any) -> Any:
        return self.connection.execute(sql, parameters).fetchone()[0]

    def snapshot(self) -> dict[str, list[tuple[Any, ...]]]:
        return {
            table: [tuple(row) for row in self.connection.execute(f"SELECT * FROM {table} ORDER BY 1")]
            for table in ("tasks", "assets", "task_events", "batches", "batch_items")
        }

    def test_compaction_keeps_inline_images_an_unfinished_submission_is_replayed_from(self) -> None:
        self.add_task("unfinished", "submitting", OLD)
        self.add_task("finished", "succeeded", OLD)
        self.add_batch("open", "active", OLD, "pending")
        self.add_batch("done", "completed", OLD, "succeeded")
        self.connection.commit()
        replayable = {
            "task": self.value("SELECT request_json FROM tasks WHERE local_id = 'unfinished'"),
            "item": self.value("SELECT body_json FROM batch_items WHERE batch_id = 'open'"),
            "template": self.value("SELECT template_json FROM batches WHERE id = 'open'"),
        }

        result = maintenance.compact_state(self.connection)
        self.connection.commit()

        self.assertEqual({key: result[key] for key in ("tasks", "batch_items", "batches")},
                         {"tasks": 1, "batch_items": 1, "batches": 1})
        self.assertGreater(result["bytes_saved"], len(IMAGE) * 3)
        self.assertEqual(replayable, {
            "task": self.value("SELECT request_json FROM tasks WHERE local_id = 'unfinished'"),
            "item": self.value("SELECT body_json FROM batch_items WHERE batch_id = 'open'"),
            "template": self.value("SELECT template_json FROM batches WHERE id = 'open'"),
        })
        finished = json.loads(self.value("SELECT request_json FROM tasks WHERE local_id = 'finished'"))
        self.assertEqual(finished["input"], {"prompt": "提示词 finished", "reference_images": [OMITTED]})
        for stored in (
            self.value("SELECT body_json FROM batch_items WHERE batch_id = 'done'"),
            self.value("SELECT template_json FROM batches WHERE id = 'done'"),
        ):
            self.assertNotIn(";base64,", stored)
        self.assertEqual(maintenance.compact_state(self.connection)["bytes_saved"], 0)

    def test_slim_json_is_idempotent_and_keeps_the_serialization_idempotency_compares(self) -> None:
        request = body("一只猫")
        untouched = dump(request)

        slim = maintenance.slim_payload(request)
        slimmed = maintenance.slim_json(json.dumps(request, indent=2))

        self.assertEqual(dump(request), untouched)
        self.assertEqual(slim["input"], {"prompt": "一只猫", "reference_images": [OMITTED]})
        self.assertEqual(maintenance.slim_payload(slim), slim)
        self.assertEqual(slimmed, dump(slim))
        self.assertIsNone(maintenance.slim_json(slimmed))

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
        self.assertEqual(preview["compacted"]["tasks"], 4)
        self.assertEqual(self.snapshot(), before)

        pruned = maintenance.prune_state(older_than_days=30)

        self.assertEqual(pruned["deleted"], expected)
        self.assertEqual(pruned["compacted"], preview["compacted"])
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
