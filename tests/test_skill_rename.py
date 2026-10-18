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

from asxlib import cli, config, state, updates
from asxlib.constants import VERSION
from asxlib.update_files import installation_problem, write_json

import install as installer


class SkillRenameTestCase(unittest.TestCase):
    def test_runtime_notice_lists_only_fixed_legacy_paths_without_touching_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            paths = [home / agent / "skills" / "asx" for agent in (".agents", ".claude", ".codex")]
            with patch("pathlib.Path.home", return_value=home):
                self.assertIsNone(config.legacy_installation_notice())
                for path in paths:
                    path.mkdir(parents=True)
                    (path / "SKILL.md").write_text("old skill", encoding="utf-8")
                unrelated = home / ".agents" / "skills" / "other-skill"
                unrelated.mkdir()
                notice = config.legacy_installation_notice()
                assert notice is not None
                self.assertEqual(notice["paths"], [str(path) for path in paths])
                for path in paths:
                    self.assertEqual((path / "SKILL.md").read_text(encoding="utf-8"), "old skill")
                    (path / "SKILL.md").unlink()
                    path.rmdir()
                self.assertIsNone(config.legacy_installation_notice())
                self.assertTrue(unrelated.is_dir())

    def test_notice_is_added_after_execution_without_changing_failure_or_exit_code(self) -> None:
        def check(payload: dict[str, object], code: int) -> None:
            events: list[str] = []

            def execute(*args: object, **kwargs: object) -> tuple[dict[str, object], int]:
                events.append("execute")
                return dict(payload), code

            def notice() -> dict[str, object]:
                events.append("notice")
                return {"paths": ["legacy/asx"]}

            with (
                patch.object(sys, "argv", ["asynx.py", "generate", "--prompt", "cat", "--detach"]),
                patch.object(cli, "execute", side_effect=execute),
                patch.object(cli, "legacy_installation_notice", side_effect=notice),
                patch.object(cli, "emit") as emit,
                self.assertRaises(SystemExit) as raised,
            ):
                cli.main()
            self.assertEqual(raised.exception.code, code)
            self.assertEqual(events, ["execute", "notice"])
            actual = dict(emit.call_args.args[0])
            self.assertEqual(actual.pop("legacy_installation_notice"), {"paths": ["legacy/asx"]})
            self.assertEqual(actual, payload)

        check({"ok": True, "status": "queued"}, 0)
        check({"ok": False, "error": {"code": "test"}}, 2)

    def test_unreadable_legacy_paths_do_not_fail_commands(self) -> None:
        with patch("pathlib.Path.exists", side_effect=PermissionError("denied")):
            self.assertIsNone(config.legacy_installation_notice())

    @unittest.skipIf(os.name == "nt", "Creating symlinks can require Windows privileges")
    def test_runtime_notice_includes_broken_link_without_following_or_removing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            legacy = home / ".agents" / "skills" / "asx"
            legacy.parent.mkdir(parents=True)
            legacy.symlink_to(home / "missing")
            with patch("pathlib.Path.home", return_value=home):
                notice = config.legacy_installation_notice()
            assert notice is not None
            self.assertEqual(notice["paths"], [str(legacy)])
            self.assertTrue(legacy.is_symlink())

    def test_legacy_install_blocks_both_targets_before_any_writes(self) -> None:
        for agent in (".agents", ".claude", ".codex"):
            with self.subTest(agent=agent), tempfile.TemporaryDirectory() as directory:
                home = Path(directory).resolve()
                old = home / agent / "skills" / "asx"
                old.mkdir(parents=True)
                marker = old / "SKILL.md"
                marker.write_text("legacy skill", encoding="utf-8")
                with self.assertRaises(RuntimeError) as raised:
                    installer.run(
                        ["--target", "both", "--skip-config"],
                        home=home,
                        install_dependencies=False,
                    )
                self.assertIn(str(old), str(raised.exception))
                self.assertEqual(marker.read_text(encoding="utf-8"), "legacy skill")
                for target in ("codex", "claude"):
                    self.assertFalse(installer._target_path(target, home).exists())

    def test_legacy_check_is_scoped_to_selected_agent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".claude" / "skills" / "asx").mkdir(parents=True)
            installer._check_legacy_installations(home, ["codex"])

    def test_fresh_install_reuses_key_and_tasks_and_uninstall_preserves_them(self) -> None:
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
                        idempotency_key="asx-before-rename",
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

    def test_old_update_cache_and_rollback_journal_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            old_cache = root / "updates.json"
            old_journal = root / "update-transaction.json"
            write_json(old_cache, {
                "check_status": "available", "release": {"version": "99.0.0"}
            })
            write_json(old_journal, {"status": "committing", "entries": []})
            before = (old_cache.read_bytes(), old_journal.read_bytes())
            with patch.object(updates, "cache_path", return_value=root / "models.json"), patch.object(
                updates, "_candidates", return_value={}
            ):
                status = updates.update_status()
                self.assertEqual(status["check_status"], "not_checked")
                self.assertIsNone(status["latest_version"])
                self.assertFalse(status["recovery_required"])
                self.assertFalse(status["rollback_available"])
                self.assertEqual(updates.update_rollback()["restored"], [])
            self.assertEqual(before, (old_cache.read_bytes(), old_journal.read_bytes()))


if __name__ == "__main__":
    unittest.main()
