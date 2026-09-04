from __future__ import annotations

import json
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "asx" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from asxlib import state
from asxlib.errors import AsxError


class StateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        state.init_schema(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _task(self, *, key: str = "asx-key") -> dict[str, object]:
        return state.create_task_intent(
            self.connection,
            operation="generate",
            model="gpt-image-2",
            idempotency_key=key,
            request={"prompt": "a keyboard"},
            output_dir=str(self.directory),
            base_url="https://example.test/api",
        )

    def test_init_schema_is_versioned_and_does_not_create_batch_tables(self) -> None:
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 1)
        tables = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue({"tasks", "assets", "task_events"}.issubset(tables))
        self.assertNotIn("batches", tables)
        self.assertNotIn("batch_items", tables)
        self.assertEqual(self.connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        columns = {
            row["name"] for row in self.connection.execute("PRAGMA table_info(tasks)")
        }
        self.assertIn("billing", columns)
        # Connection pragmas are applied by connect_db; init_schema also works
        # with a caller-owned connection for transaction composition.

    def test_connect_db_creates_private_directory_and_file(self) -> None:
        database = self.directory / "nested" / "state.db"
        connection = state.connect_db(database)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
        finally:
            connection.close()
        self.assertEqual(stat.S_IMODE(database.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(database.stat().st_mode), 0o600)

    def test_legacy_unversioned_database_gets_v1_schema(self) -> None:
        database = self.directory / "legacy.db"
        old = sqlite3.connect(database)
        old.execute("CREATE TABLE artifacts (id INTEGER PRIMARY KEY, path TEXT)")
        old.commit()
        old.close()

        connection = state.connect_db(database)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_incompatible_legacy_core_tables_are_rebuilt(self) -> None:
        database = self.directory / "legacy-core.db"
        old = sqlite3.connect(database)
        old.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, status TEXT)")
        old.execute("CREATE TABLE batches (id TEXT PRIMARY KEY)")
        old.commit()
        old.close()

        connection = state.connect_db(database)
        try:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(tasks)")
            }
            self.assertIn("local_id", columns)
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='batches'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_task_intent_is_idempotent_and_conflicts_are_rejected(self) -> None:
        first = self._task()
        replay = self._task()
        self.assertEqual(first["local_id"], replay["local_id"])
        with self.assertRaises(AsxError) as caught:
            state.create_task_intent(
                self.connection,
                operation="edit",
                model="gpt-image-2",
                idempotency_key="asx-key",
                request={"prompt": "different"},
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_bind_update_and_events_round_trip_json(self) -> None:
        task = self._task()
        bound = state.bind_remote_task(
            self.connection,
            str(task["local_id"]),
            "remote-1",
            remote={"id": "remote-1", "status": "queued"},
            status="queued",
        )
        self.assertEqual(bound["remote_task_id"], "remote-1")
        updated = state.update_task(
            self.connection,
            str(task["local_id"]),
            status="running",
            billing={"estimated": "0.12"},
            last_polled="2026-09-04T10:00:00+00:00",
        )
        self.assertEqual(updated["billing"], {"estimated": "0.12"})
        event = state.append_event(
            self.connection,
            str(task["local_id"]),
            "status_changed",
            {"from": "queued", "to": "running"},
        )
        self.assertEqual(event["payload"]["to"], "running")
        events = self.connection.execute(
            "SELECT event_type, payload_json FROM task_events WHERE local_task_id = ?",
            (task["local_id"],),
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["remote_bound", "status_changed"])
        self.assertEqual(json.loads(events[-1][1])["from"], "queued")

    def test_assets_have_stable_id_and_upsert_without_duplicates(self) -> None:
        task = self._task()
        image = self.directory / "keyboard.webp"
        image.write_bytes(b"webp")
        first = state.upsert_asset(
            self.connection,
            str(task["local_id"]),
            0,
            path=str(image),
            remote_url="https://cdn.example.test/1",
            mime="image/webp",
            size_bytes=4,
            sha256="a" * 64,
        )
        second = state.upsert_asset(
            self.connection,
            str(task["local_id"]),
            0,
            path=str(image),
            remote_url="https://cdn.example.test/2",
            mime="image/webp",
            bytes=8,
            sha256="b" * 64,
            state="available",
        )
        self.assertEqual(first["artifact_id"], second["artifact_id"])
        self.assertEqual(second["bytes"], 8)
        self.assertEqual(len(state.list_assets(self.connection, str(task["local_id"]))), 1)
        self.assertEqual(state.task_paths(self.connection, str(task["local_id"])), [str(image)])
        state.bind_remote_task(self.connection, str(task["local_id"]), "remote-assets")
        self.assertEqual(state.task_paths(self.connection, "remote-assets"), [str(image)])

    def test_missing_asset_path_is_reported(self) -> None:
        task = self._task()
        missing = self.directory / "missing.png"
        state.upsert_asset(self.connection, str(task["local_id"]), 0, path=str(missing))
        with self.assertRaises(AsxError) as caught:
            state.task_paths(self.connection, str(task["local_id"]))
        self.assertEqual(caught.exception.code, "asset_file_missing")

    def test_task_filters_and_remote_lookup(self) -> None:
        first = self._task(key="asx-one")
        self._task(key="asx-two")
        state.bind_remote_task(self.connection, str(first["local_id"]), "remote-1")
        state.update_task(self.connection, str(first["local_id"]), status="running")
        remote = state.get_task(self.connection, remote_task_id="remote-1")
        self.assertIsNotNone(remote)
        assert remote is not None
        self.assertEqual(remote["local_id"], first["local_id"])
        self.assertEqual(len(state.list_tasks(self.connection, status="running")), 1)
        self.assertEqual(len(state.list_tasks(self.connection, operation="generate", limit=1)), 1)


if __name__ == "__main__":
    unittest.main()
