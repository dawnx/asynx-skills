from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills" / "asynx-image" / "scripts"))

from asxlib import config, state
from asxlib.constants import VERSION
from asxlib.update_files import installation_problem

import install as installer


class InstallLifecycleTestCase(unittest.TestCase):
    def test_install_reuses_key_and_tasks_and_uninstall_preserves_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            environment = {
                name: value
                for name, value in os.environ.items()
                if not name.startswith("ASYNX_")
                and name not in {"XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"}
            }
            environment.update(
                HOME=str(home),
                USERPROFILE=str(home),
                APPDATA=str(home / "AppData" / "Roaming"),
                LOCALAPPDATA=str(home / "AppData" / "Local"),
                ASYNX_UPDATE_CHECK="off",
            )
            with patch.dict(os.environ, environment, clear=True), patch(
                "pathlib.Path.home", return_value=home
            ), contextlib.redirect_stdout(io.StringIO()):
                credential_path = config.config_path()
                credential_path.parent.mkdir(parents=True)
                credential_bytes = b'{"api_key":"asx-test-preserved"}\n'
                credential_path.write_bytes(credential_bytes)
                database = config.state_path()
                connection = state.connect_db(database)
                try:
                    task = state.create_task_intent(
                        connection,
                        operation="generate",
                        model="gpt-image-2",
                        idempotency_key="asx-before-install",
                        request={"prompt": "existing task"},
                        output_dir=str(home / "images"),
                        base_url="https://example.test/api",
                    )
                    connection.commit()
                finally:
                    connection.close()
                before_database = database.read_bytes()
                result = installer.run(
                    ["--target", "both", "--skip-config"],
                    home=home,
                    install_dependencies=False,
                )
                self.assertEqual(result, 0)
                self.assertEqual(credential_path.read_bytes(), credential_bytes)
                self.assertEqual(database.read_bytes(), before_database)
                for target in ("codex", "claude"):
                    root = installer._target_path(target, home)
                    self.assertEqual(root.name, "asynx-image")
                    self.assertIsNone(installation_problem(root))
                    script = root / "scripts" / "asynx.py"
                    for arguments in (("--version",), ("task", "list")):
                        process = subprocess.run(
                            [sys.executable, "-B", str(script), *arguments],
                            env=environment,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=20,
                        )
                        self.assertEqual(process.returncode, 0, process.stderr)
                        if arguments == ("--version",):
                            self.assertEqual(process.stdout.strip(), VERSION)
                        else:
                            self.assertIn(task["local_id"], process.stdout)
                    self.assertTrue(installer._uninstall_skill(root))
                self.assertEqual(credential_path.read_bytes(), credential_bytes)
                self.assertTrue(database.is_file())
                connection = state.connect_db(database)
                try:
                    self.assertIsNotNone(state.get_task(connection, task["local_id"]))
                finally:
                    connection.close()


if __name__ == "__main__":
    unittest.main()
