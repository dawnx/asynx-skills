from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "asx" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from asxlib import artifacts
from asxlib.errors import AsxError


class ArtifactsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _file(self, name: str, content: bytes = b"image") -> Path:
        path = self.directory / name
        path.write_bytes(content)
        return path

    def test_init_schema_is_idempotent_and_recorded_assets_are_listed(self) -> None:
        artifacts.init_schema(self.connection)
        artifacts.init_schema(self.connection)

        first = self._file("sunrise.png")
        second = self._file("sunset.png")
        artifacts.record_artifacts(self.connection, "task-001", [str(first), str(second)])
        self.connection.commit()

        rows = artifacts.list_artifacts(self.connection)

        self.assertEqual(len(rows), 2)
        self.assertEqual({row["task_id"] for row in rows}, {"task-001"})
        self.assertEqual({row["path"] for row in rows}, {str(first), str(second)})
        self.assertTrue(all(row["missing"] is False for row in rows))

    def test_recording_same_task_is_idempotent_and_artifact_paths_preserve_order(self) -> None:
        artifacts.init_schema(self.connection)
        first = self._file("first.webp")
        second = self._file("second.webp")

        artifacts.record_artifacts(self.connection, "task-002", [str(first), str(second)])
        artifacts.record_artifacts(self.connection, "task-002", [str(first), str(second)])
        self.connection.commit()

        self.assertEqual(
            artifacts.artifact_paths(self.connection, "task-002"),
            [str(first), str(second)],
        )
        self.assertEqual(len(artifacts.list_artifacts(self.connection, task_id="task-002")), 2)

    def test_list_artifacts_supports_query_task_filter_and_limit(self) -> None:
        artifacts.init_schema(self.connection)
        sunrise = self._file("sunrise.png")
        sunset = self._file("sunset.png")
        cat = self._file("cat.png")
        artifacts.record_artifacts(self.connection, "task-sun", [str(sunrise), str(sunset)])
        artifacts.record_artifacts(self.connection, "task-cat", [str(cat)])
        self.connection.commit()

        query_rows = artifacts.list_artifacts(self.connection, query="sun")
        self.assertEqual({row["path"] for row in query_rows}, {str(sunrise), str(sunset)})

        task_rows = artifacts.list_artifacts(self.connection, task_id="task-cat")
        self.assertEqual([row["path"] for row in task_rows], [str(cat)])

        limited_rows = artifacts.list_artifacts(self.connection, query=".png", limit=2)
        self.assertEqual(len(limited_rows), 2)
        self.assertEqual(artifacts.list_artifacts(self.connection, query="missing"), [])

    def test_missing_file_is_marked_in_list_and_errors_when_paths_are_requested(self) -> None:
        artifacts.init_schema(self.connection)
        existing = self._file("existing.png")
        missing = self.directory / "deleted.png"
        artifacts.record_artifacts(self.connection, "task-missing", [str(existing), str(missing)])
        self.connection.commit()

        missing.unlink(missing_ok=True)
        rows = artifacts.list_artifacts(self.connection, task_id="task-missing")
        by_path = {row["path"]: row for row in rows}
        self.assertFalse(by_path[str(existing)]["missing"])
        self.assertTrue(by_path[str(missing)]["missing"])

        with self.assertRaises(AsxError) as caught:
            artifacts.artifact_paths(self.connection, "task-missing")
        self.assertEqual(caught.exception.code, "artifact_file_missing")
        self.assertIn(str(missing), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
