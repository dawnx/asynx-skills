from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "skills" / "asx"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(SOURCE / "scripts"))

from asx_runtime import file_lock, runtime_lock_path
from asxlib import cli, updates
from asxlib.errors import AsxError
from asxlib.update_files import (
    RECEIPT,
    extract_package,
    installation_problem,
    inventory,
    prepare_dependencies,
    read_json,
    read_version,
    record_install,
    restore_transaction,
    validate_manifest,
    version_tuple,
    write_json,
)
from build_release import build_release

import install as installer


class UpdateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.codex = self.root / "user" / ".agents" / "skills" / "asx"
        self.claude = self.root / "user" / ".claude" / "skills" / "asx"
        self.cache = self.root / "cache" / "updates.json"
        self.journal = self.root / "cache" / "update-transaction.json"
        self.package = self.root / "package"
        self.copy_skill(self.package, "0.6.0")
        archive, manifest = build_release(self.package, self.root / "release")
        self.archive = archive.read_bytes()
        self.manifest = json.loads(manifest.read_text())
        self.release = updates._release_descriptor("0.6.0")
        self.env = os.environ.copy()
        for name in tuple(self.env):
            if name.startswith("ASYNX_"):
                self.env.pop(name)
        self.env.update(
            {
                "ASYNX_API_KEY": "asx-test-credential-never-send",
                "ASYNX_CONFIG_PATH": str(self.root / "private" / "config.json"),
                "ASYNX_STATE_PATH": str(self.root / "private" / "state.db"),
            }
        )
        for patcher in (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(updates, "skill_root", return_value=self.codex),
            patch.object(
                updates,
                "known_installations",
                return_value={"codex": self.codex, "claude": self.claude},
            ),
            patch.object(updates, "update_cache_path", return_value=self.cache),
            patch.object(updates, "journal_path", return_value=self.journal),
            patch("asxlib.update_files.prepare_dependencies"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def copy_skill(path: Path, version: str) -> None:
        shutil.copytree(
            SOURCE,
            path,
            ignore=shutil.ignore_patterns("vendor", "__pycache__", "*.pyc", RECEIPT),
        )
        constants = path / "scripts" / "asxlib" / "constants.py"
        content = constants.read_text(encoding="utf-8")
        current = read_version(path)
        constants.write_text(
            content.replace(f'VERSION = "{current}"', f'VERSION = "{version}"'),
            encoding="utf-8",
        )

    def install(self, path: Path, version: str = "0.4.0") -> None:
        self.copy_skill(path, version)
        (path / "scripts" / "obsolete.py").write_text("# removed in the next release\n")
        record_install(path)

    def fetch(self, url: str, **_kwargs: Any) -> bytes:
        if url == updates.LATEST_RELEASE_URL:
            return json.dumps(
                {
                    "tag_name": "v0.6.0",
                    "draft": False,
                    "prerelease": False,
                    "assets": [{"name": "asx-0.6.0.json"}, {"name": "asx-0.6.0.zip"}],
                }
            ).encode()
        if url == self.release["manifest_url"]:
            return json.dumps(self.manifest).encode()
        if url == self.release["archive_url"]:
            return self.archive
        raise AssertionError(f"Unexpected release request: {url}")

    def test_status_is_offline_and_lists_versions_without_loading_credentials(
        self,
    ) -> None:
        self.install(self.codex)
        self.install(self.claude, "0.3.0")
        with patch.object(
            updates, "_fetch", side_effect=AssertionError("network forbidden")
        ):
            result = updates.update_status()
        self.assertIsNone(result["update_available"])
        self.assertEqual(result["check_status"], "not_checked")
        self.assertEqual(
            [i["version"] for i in result["installed_copies"]], ["0.4.0", "0.3.0"]
        )
        self.assertNotIn("asx-test-credential-never-send", json.dumps(result))
        self.assertFalse(self.cache.exists())

    def test_status_distinguishes_untracked_and_modified_installs(self) -> None:
        self.install(self.codex)
        self.install(self.claude)
        (self.codex / "SKILL.md").write_text("user change")
        (self.claude / RECEIPT).unlink()
        result = updates.update_status()
        self.assertEqual(
            [i["update_blocked_by"] for i in result["installed_copies"]],
            ["local_changes", "untracked"],
        )

    def test_installer_tracks_source_files_without_adopting_user_files(self) -> None:
        installer._install_skill(self.codex, install_dependencies=False)
        self.assertIsNone(installation_problem(self.codex))
        (self.codex / "personal-note.txt").write_text("keep me")
        installer._install_skill(self.codex, install_dependencies=False)
        self.assertEqual(installation_problem(self.codex), "local_changes")
        self.assertEqual((self.codex / "personal-note.txt").read_text(), "keep me")

    def test_check_uses_six_hour_cache_then_refreshes_and_force_bypasses(self) -> None:
        with patch.object(updates, "_fetch", side_effect=self.fetch) as fetch:
            first = updates.update_check()
            self.assertTrue(first["update_available"])
            self.assertTrue(updates.update_check()["cached"])
            self.assertEqual(fetch.call_count, 1)
            cache = read_json(self.cache)
            cache["checked_at"] = time.time() - 6 * 60 * 60 - 1
            write_json(self.cache, cache)
            self.assertFalse(updates.update_check()["cached"])
            updates.update_check(force=True)
            self.assertEqual(fetch.call_count, 3)

    def test_clock_moving_backwards_invalidates_cache(self) -> None:
        write_json(
            self.cache,
            {
                "checked_at": time.time() + 100,
                "check_status": "available",
                "release": self.release,
            },
        )
        with patch.object(updates, "_fetch", side_effect=self.fetch) as fetch:
            updates.update_check()
            fetch.assert_called_once()

    def test_same_release_notice_repeats_without_another_network_check(self) -> None:
        self.install(self.codex)
        with patch.object(updates, "_fetch", side_effect=self.fetch) as fetch:
            first = updates.automatic_update_notice()
            second = updates.automatic_update_notice()
            assert first is not None and second is not None
            self.assertEqual(first["latest_version"], "0.6.0")
            self.assertEqual(second["latest_version"], "0.6.0")
            fetch.assert_called_once()

    def test_automatic_check_skips_source_copies_and_can_be_disabled(self) -> None:
        with patch.object(
            updates, "_fetch", side_effect=AssertionError("network forbidden")
        ):
            self.assertIsNone(updates.automatic_update_notice())
            self.install(self.codex)
            with patch.dict(os.environ, {"ASYNX_UPDATE_CHECK": "off"}):
                self.assertIsNone(updates.automatic_update_notice())

    def test_offline_failure_is_cached_and_is_not_reported_as_latest(self) -> None:
        self.install(self.codex)
        with patch.object(updates, "_fetch", side_effect=TimeoutError) as fetch:
            self.assertIsNone(updates.automatic_update_notice())
            self.assertIsNone(updates.automatic_update_notice())
            fetch.assert_called_once()
        status = updates.update_status()
        self.assertEqual(status["check_status"], "unavailable")
        self.assertIsNone(status["update_available"])

    def test_corrupt_cache_and_future_metadata_do_not_break_checks(self) -> None:
        self.cache.parent.mkdir()
        self.cache.write_text("not json")
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            self.assertTrue(updates.update_check()["ok"])

    def test_prerelease_incomplete_release_and_bad_tag_are_rejected(self) -> None:
        cases: list[Any] = [
            {"tag_name": "v0.6.0-rc1"},
            {"tag_name": "v0.6.0", "prerelease": True},
            {"tag_name": "v0.6.0", "draft": True},
            {"tag_name": "v0.6.0", "assets": []},
            [],
        ]
        for data in cases:
            with (
                self.subTest(data=data),
                patch.object(updates, "_fetch", return_value=json.dumps(data).encode()),
            ):
                result = updates.update_check(force=True)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["update_available"])

    def test_apply_updates_both_copies_preserves_data_and_can_roll_back(self) -> None:
        self.install(self.codex)
        self.install(self.claude, "0.3.0")
        private = self.root / "private"
        private.mkdir()
        for name in ("config.json", "state.db", "output.png"):
            (private / name).write_bytes(b"private-user-data")
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            result = updates.update_apply()
        self.assertTrue(result["updated"])
        self.assertEqual(len(result["installations"]), 2)
        for root in (self.codex, self.claude):
            self.assertEqual(read_version(root), "0.6.0")
            self.assertIsNone(installation_problem(root))
            self.assertFalse((root / "scripts" / "obsolete.py").exists())
            proc = subprocess.run(
                [sys.executable, str(root / "scripts" / "asynx.py"), "--version"],
                capture_output=True,
                encoding="utf-8",
                check=False,
            )
            self.assertEqual(proc.stdout.strip(), "0.6.0", proc.stderr)
        for path in private.iterdir():
            self.assertEqual(path.read_bytes(), b"private-user-data")
        self.assertTrue(updates.update_status()["rollback_available"])
        rollback = updates.update_rollback()
        self.assertEqual(set(rollback["restored"]), {str(self.codex), str(self.claude)})
        self.assertEqual(read_version(self.codex), "0.4.0")
        self.assertEqual(read_version(self.claude), "0.3.0")

    def test_target_selection_does_not_install_absent_agents(self) -> None:
        self.install(self.codex)
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            updates.update_apply(target="codex")
        self.assertFalse(self.claude.exists())
        self.assertEqual(read_version(self.codex), "0.6.0")

    def test_only_selected_agent_is_updated(self) -> None:
        self.install(self.codex)
        self.install(self.claude)
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            updates.update_apply(target="claude")
        self.assertEqual(read_version(self.codex), "0.4.0")
        self.assertEqual(read_version(self.claude), "0.6.0")

    def test_private_data_inside_installation_blocks_even_forced_update(self) -> None:
        self.install(self.codex)
        private = self.codex / "config.json"
        private.write_text("private credential")
        with (
            patch.dict(os.environ, {"ASYNX_CONFIG_PATH": str(private)}),
            patch.object(
                updates, "_fetch", side_effect=AssertionError("network forbidden")
            ),
            self.assertRaises(AsxError) as error,
        ):
            updates.update_apply(force=True)
        self.assertEqual(error.exception.code, "update_data_inside_installation")
        self.assertEqual(private.read_text(), "private credential")

    def test_installed_entrypoint_can_update_itself(self) -> None:
        self.install(self.codex)
        script = self.codex / "scripts" / "asynx.py"
        release_dir = self.root / "release"
        harness = (
            "import json,pathlib,runpy,sys; "
            "sys.path.insert(0, sys.argv[1]+'/scripts'); "
            "from asxlib import updates,update_files; "
            "root=pathlib.Path(sys.argv[1]); release=pathlib.Path(sys.argv[2]); "
            "updates.known_installations=lambda:{'codex':root}; "
            "updates.update_cache_path=lambda:release/'updates.json'; "
            "updates.journal_path=lambda:release/'transaction.json'; "
            "latest=json.dumps({'tag_name':'v0.6.0','assets':[{'name':'asx-0.6.0.zip'},{'name':'asx-0.6.0.json'}]}).encode(); "
            "updates._fetch=lambda url,**kw:latest if url==updates.LATEST_RELEASE_URL else (release/url.rsplit('/',1)[-1]).read_bytes(); "
            "update_files.prepare_dependencies=lambda *args:None; "
            "sys.argv=[str(root/'scripts'/'asynx.py'),'update','apply','--target','current']; "
            "runpy.run_path(sys.argv[0],run_name='__main__')"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", harness, str(self.codex), str(release_dir)],
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertTrue(json.loads(result.stdout)["updated"])
        self.assertEqual(read_version(self.codex), "0.6.0")
        self.assertTrue(script.is_file())

    def test_bad_notice_metadata_does_not_lose_the_completed_result(self) -> None:
        self.install(self.codex)
        write_json(self.journal, {"status": {"unexpected": "type"}})
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            self.assertIsNone(updates.automatic_update_notice())

    def test_modified_backup_is_not_restored(self) -> None:
        self.install(self.codex)
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            result = updates.update_apply()
        backup = Path(result["installations"][0]["backup_path"])
        (backup / "SKILL.md").write_text("modified backup")
        with self.assertRaises(AsxError) as error:
            updates.update_rollback()
        self.assertEqual(error.exception.code, "update_backup_changed")
        self.assertEqual(read_version(self.codex), "0.6.0")

    def test_interrupted_explicit_rollback_can_finish_on_next_command(self) -> None:
        self.install(self.codex)
        self.install(self.claude)
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            updates.update_apply()
        rename = Path.rename

        def interrupt_first_backup(path: Path, target: Path) -> Path:
            if path.name == "backup" and target == self.codex:
                raise PermissionError("backup is temporarily locked")
            return rename(path, target)

        with (
            patch.object(Path, "rename", interrupt_first_backup),
            self.assertRaises(AsxError),
        ):
            updates.update_rollback()
        self.assertEqual(read_json(self.journal)["status"], "rolling_back")
        self.assertEqual(read_version(self.claude), "0.4.0")
        updates.update_rollback()
        self.assertEqual(read_version(self.codex), "0.4.0")

    def test_corrupt_backup_stops_rollback_without_replacing_new_version(self) -> None:
        self.install(self.codex)
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            result = updates.update_apply()
        backup = Path(result["installations"][0]["backup_path"])
        backup.rename(backup.with_name("saved_elsewhere"))
        with self.assertRaises(AsxError) as error:
            updates.update_rollback()
        self.assertEqual(error.exception.code, "update_backup_missing")
        self.assertEqual(read_version(self.codex), "0.6.0")

    def test_user_edit_after_update_is_protected_during_rollback(self) -> None:
        self.install(self.codex)
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            updates.update_apply()
        (self.codex / "SKILL.md").write_text("new user instructions")
        with self.assertRaises(AsxError) as error:
            updates.update_rollback()
        self.assertEqual(error.exception.code, "local_changes")
        self.assertEqual((self.codex / "SKILL.md").read_text(), "new user instructions")

    def test_download_failure_preserves_existing_copy(self) -> None:
        self.install(self.codex)
        before = inventory(self.codex)

        def fetch(url: str, **kwargs: Any) -> bytes:
            if url == self.release["archive_url"]:
                raise TimeoutError("download interrupted")
            return self.fetch(url, **kwargs)

        with (
            patch.object(updates, "_fetch", side_effect=fetch),
            self.assertRaises(AsxError),
        ):
            updates.update_apply()
        self.assertEqual(inventory(self.codex), before)

    def test_concurrent_update_fails_before_downloading(self) -> None:
        self.install(self.codex)
        with (
            file_lock(self.journal.with_suffix(".lock")),
            patch.object(
                updates, "_fetch", side_effect=AssertionError("network forbidden")
            ),
            self.assertRaises(AsxError),
        ):
            updates.update_apply()

    def test_changed_files_during_staging_are_not_overwritten(self) -> None:
        self.install(self.codex)

        def change(_stage: Path, previous: Path) -> None:
            (previous / "SKILL.md").write_text("changed during staging")

        with (
            patch.object(updates, "_fetch", side_effect=self.fetch),
            patch("asxlib.update_files.prepare_dependencies", side_effect=change),
            self.assertRaises(AsxError) as error,
        ):
            updates.update_apply()
        self.assertEqual(error.exception.code, "local_changes")
        self.assertEqual(
            (self.codex / "SKILL.md").read_text(), "changed during staging"
        )

    def test_other_agent_can_still_trigger_notice_when_current_is_latest(self) -> None:
        self.install(self.codex, "0.6.0")
        self.install(self.claude)
        with (
            patch.object(updates, "VERSION", "0.6.0"),
            patch.object(updates, "_fetch", side_effect=self.fetch),
        ):
            self.assertTrue(updates.update_check()["update_available"])
            self.assertIsNotNone(updates.automatic_update_notice())

    def test_full_update_through_local_http_release_service(self) -> None:
        self.install(self.codex)
        self.install(self.claude)
        urls = {
            "/latest": updates.LATEST_RELEASE_URL,
            "/manifest": self.release["manifest_url"],
            "/archive": self.release["archive_url"],
        }
        owner = self
        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:
                requests.append(self.path)
                owner.assertIsNone(self.headers.get("Authorization"))
                raw = owner.fetch(urls[self.path])
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        fetch = updates._fetch

        def local_fetch(url: str, **kwargs: Any) -> bytes:
            route = next(path for path, remote in urls.items() if remote == url)
            return fetch(f"http://127.0.0.1:{server.server_port}{route}", **kwargs)

        try:
            with (
                patch.object(updates, "_fetch", side_effect=local_fetch),
                patch.object(updates, "_validate_download_url"),
            ):
                result = updates.update_apply()
            self.assertTrue(result["updated"])
            self.assertEqual(requests, ["/latest", "/manifest", "/archive"])
            self.assertEqual(read_version(self.codex), "0.6.0")
            self.assertEqual(read_version(self.claude), "0.6.0")
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_same_and_older_releases_never_downgrade(self) -> None:
        self.install(self.codex, "0.7.0")
        self.install(self.claude, "0.6.0")
        with patch.object(updates, "_fetch", side_effect=self.fetch) as fetch:
            self.assertFalse(updates.update_apply()["updated"])
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(read_version(self.codex), "0.7.0")

    def test_modified_files_require_force_and_are_backed_up(self) -> None:
        self.install(self.codex)
        (self.codex / "SKILL.md").write_text("user instructions")
        with patch.object(updates, "_fetch", side_effect=self.fetch):
            with self.assertRaises(AsxError) as error:
                updates.update_apply()
            self.assertEqual(error.exception.code, "local_changes")
            result = updates.update_apply(force=True)
        backup = Path(result["installations"][0]["backup_path"])
        self.assertEqual((backup / "SKILL.md").read_text(), "user instructions")
        updates.update_rollback()
        self.assertEqual((self.codex / "SKILL.md").read_text(), "user instructions")

    def test_untracked_symlink_and_source_checkout_are_never_overwritten(self) -> None:
        self.install(self.codex)
        (self.codex / RECEIPT).unlink()
        with patch.object(
            updates, "_fetch", side_effect=AssertionError("network forbidden")
        ):
            with self.assertRaises(AsxError) as error:
                updates.update_apply(force=True)
            self.assertEqual(error.exception.code, "untracked")
        record_install(self.codex)
        (self.codex / ".git").mkdir()
        with self.assertRaises(AsxError) as error:
            updates.update_apply(target="current", force=True)
        self.assertEqual(error.exception.code, "source_checkout")

    @unittest.skipIf(os.name == "nt", "symlink privilege varies on Windows")
    def test_symlinked_install_is_not_followed(self) -> None:
        self.install(self.claude)
        self.codex.parent.mkdir(parents=True)
        self.codex.symlink_to(self.claude, target_is_directory=True)
        with self.assertRaises(AsxError) as error:
            updates.update_apply(target="codex", force=True)
        self.assertEqual(error.exception.code, "symlink")
        self.assertEqual(read_version(self.claude), "0.4.0")

    def test_bad_checksum_rejects_before_touching_installation(self) -> None:
        self.install(self.codex)
        before = inventory(self.codex)
        self.manifest["sha256"] = "0" * 64
        with (
            patch.object(updates, "_fetch", side_effect=self.fetch),
            self.assertRaises(AsxError) as error,
        ):
            updates.update_apply()
        self.assertEqual(error.exception.code, "update_checksum_mismatch")
        self.assertEqual(inventory(self.codex), before)
        self.assertFalse(self.journal.exists())

    def test_unsupported_python_rejects_before_download(self) -> None:
        self.install(self.codex)
        self.manifest["python_min"] = "3.99"
        with (
            patch.object(updates, "_fetch", side_effect=self.fetch) as fetch,
            self.assertRaises(AsxError) as error,
        ):
            updates.update_apply()
        self.assertEqual(error.exception.code, "unsupported_update_python")
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(read_version(self.codex), "0.4.0")

    def test_dependency_failure_leaves_every_installation_unchanged(self) -> None:
        self.install(self.codex)
        self.install(self.claude)
        with (
            patch.object(updates, "_fetch", side_effect=self.fetch),
            patch(
                "asxlib.update_files.prepare_dependencies",
                side_effect=[None, AsxError("dependency failed")],
            ),
            self.assertRaises(AsxError),
        ):
            updates.update_apply()
        self.assertEqual(read_version(self.codex), "0.4.0")
        self.assertEqual(read_version(self.claude), "0.4.0")
        self.assertFalse(self.journal.exists())

    def test_invalid_new_code_fails_before_replacing_any_copy(self) -> None:
        self.install(self.codex)
        (self.package / "scripts" / "asynx.py").write_text("raise SystemExit(9)\n")
        archive, manifest = build_release(self.package, self.root / "bad-release")
        self.archive = archive.read_bytes()
        self.manifest = json.loads(manifest.read_text())
        with (
            patch.object(updates, "_fetch", side_effect=self.fetch),
            self.assertRaises(AsxError) as error,
        ):
            updates.update_apply()
        self.assertEqual(error.exception.code, "update_validation_failed")
        self.assertEqual(read_version(self.codex), "0.4.0")

    def test_mid_commit_failure_restores_all_copies(self) -> None:
        self.install(self.codex)
        self.install(self.claude)
        rename = Path.rename

        def fail_second_stage(path: Path, target: Path) -> Path:
            if path.name == "new" and target == self.claude:
                raise PermissionError("simulated file lock")
            return rename(path, target)

        with (
            patch.object(updates, "_fetch", side_effect=self.fetch),
            patch.object(Path, "rename", fail_second_stage),
            self.assertRaises(AsxError) as error,
        ):
            updates.update_apply()
        self.assertEqual(error.exception.code, "update_failed")
        self.assertEqual(read_version(self.codex), "0.4.0")
        self.assertEqual(read_version(self.claude), "0.4.0")
        self.assertEqual(read_json(self.journal)["status"], "rolled_back")

    def test_killed_update_recovers_a_missing_target_without_network(self) -> None:
        self.install(self.codex)
        work = Path(tempfile.mkdtemp(prefix=".asx-update-", dir=self.codex.parent))
        self.codex.rename(work / "backup")
        write_json(
            self.journal,
            {
                "status": "committing",
                "entries": [{"target": str(self.codex), "work": str(work)}],
            },
        )
        with patch.object(
            updates, "_fetch", side_effect=AssertionError("network forbidden")
        ):
            result = updates.update_apply()
        self.assertEqual(result["recovered"], [str(self.codex)])
        self.assertEqual(read_version(self.codex), "0.4.0")

    def test_killed_rollback_resumes_after_current_copy_was_moved(self) -> None:
        self.install(self.codex)
        work = Path(tempfile.mkdtemp(prefix=".asx-update-", dir=self.codex.parent))
        self.codex.rename(work / "backup")
        (work / "discarded").mkdir()
        write_json(
            self.journal,
            {
                "status": "committing",
                "entries": [{"target": str(self.codex), "work": str(work)}],
            },
        )
        self.assertEqual(
            restore_transaction(self.journal, [self.codex]), [str(self.codex)]
        )
        self.assertEqual(read_version(self.codex), "0.4.0")

    def test_forged_recovery_paths_cannot_modify_unrelated_directories(self) -> None:
        unrelated = self.root / "private"
        unrelated.mkdir()
        (unrelated / "keep").write_text("unchanged")
        write_json(
            self.journal,
            {
                "status": "committing",
                "entries": [
                    {
                        "target": str(unrelated),
                        "work": str(self.root / ".asx-update-test"),
                    }
                ],
            },
        )
        with self.assertRaises(AsxError):
            updates.update_rollback()
        self.assertEqual((unrelated / "keep").read_text(), "unchanged")

    def test_running_command_blocks_updates_before_network(self) -> None:
        self.install(self.codex)
        with (
            file_lock(runtime_lock_path(self.codex), exclusive=False),
            patch.object(
                updates, "_fetch", side_effect=AssertionError("network forbidden")
            ),
            self.assertRaises(AsxError) as error,
        ):
            updates.update_apply()
        self.assertEqual(error.exception.code, "update_busy")

    def test_entrypoint_obeys_update_lock_and_returns_json(self) -> None:
        self.install(self.codex)
        with file_lock(runtime_lock_path(self.codex)):
            proc = subprocess.run(
                [sys.executable, str(self.codex / "scripts" / "asynx.py"), "--version"],
                capture_output=True,
                encoding="utf-8",
                check=False,
            )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "update_busy")

    def test_normal_commands_can_share_runtime_lock(self) -> None:
        self.install(self.codex)
        with (
            file_lock(runtime_lock_path(self.codex), exclusive=False),
            file_lock(runtime_lock_path(self.codex), exclusive=False),
        ):
            proc = subprocess.run(
                [sys.executable, str(self.codex / "scripts" / "asynx.py"), "--version"],
                capture_output=True,
                encoding="utf-8",
                check=False,
                timeout=10,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)

    def sandboxed_entrypoint(
        self, *arguments: str, readable: bool = True
    ) -> subprocess.CompletedProcess[str]:
        """Deny installation writes even on hosts where chmod is ineffective."""
        return subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                """
import errno, pathlib, runpy, sys
from unittest.mock import patch
lock = pathlib.Path(sys.argv[1])
readable = sys.argv[2] == "read"
script = pathlib.Path(sys.argv[3])
arguments = sys.argv[4:]
original_open, original_mkdir = pathlib.Path.open, pathlib.Path.mkdir
def guarded_open(path, mode="r", *args, **kwargs):
    if path == lock and (not readable or mode not in {"r", "rb"}):
        raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
    return original_open(path, mode, *args, **kwargs)
def guarded_mkdir(path, *args, **kwargs):
    if path == lock.parent:
        raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
    return original_mkdir(path, *args, **kwargs)
sys.path.insert(0, str(script.parent))
sys.argv = [str(script), *arguments]
with patch.object(pathlib.Path, "open", guarded_open), patch.object(pathlib.Path, "mkdir", guarded_mkdir):
    runpy.run_path(str(script), run_name="__main__")
""",
                str(runtime_lock_path(self.codex)),
                "read" if readable else "deny",
                str(self.codex / "scripts" / "asynx.py"),
                *arguments,
            ],
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            check=False,
            timeout=10,
        )

    def test_installer_prepares_lock_without_replacing_an_existing_inode(self) -> None:
        installer._install_skill(self.codex, install_dependencies=False)
        lock = runtime_lock_path(self.codex)
        self.assertTrue(lock.is_file())
        lock.write_bytes(b"preserve existing lock")
        inode = lock.stat().st_ino
        installer._install_skill(self.codex, install_dependencies=False)
        self.assertEqual(lock.stat().st_ino, inode)
        self.assertEqual(lock.read_bytes(), b"preserve existing lock")

    def test_installed_commands_work_without_installation_write_access(self) -> None:
        installer._install_skill(self.codex, install_dependencies=False)
        for arguments in (("--version",), ("config", "status")):
            with self.subTest(arguments=arguments):
                proc = self.sandboxed_entrypoint(*arguments)
                self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
                if arguments == ("--version",):
                    self.assertEqual(proc.stdout.strip(), read_version(SOURCE))
                else:
                    self.assertTrue(json.loads(proc.stdout)["ok"])

    def test_unreadable_runtime_lock_is_not_reported_as_busy(self) -> None:
        installer._install_skill(self.codex, install_dependencies=False)
        proc = self.sandboxed_entrypoint("--version", readable=False)
        self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
        error = json.loads(proc.stdout)["error"]
        self.assertEqual(error["code"], "runtime_lock_error")
        self.assertEqual(error["details"]["errno"], errno.EPERM)
        self.assertEqual(error["details"]["path"], str(runtime_lock_path(self.codex)))

    def test_sandboxed_command_still_respects_an_active_update(self) -> None:
        installer._install_skill(self.codex, install_dependencies=False)
        with file_lock(runtime_lock_path(self.codex)):
            proc = self.sandboxed_entrypoint("--version")
        self.assertEqual(proc.returncode, 2, proc.stderr + proc.stdout)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "update_busy")

    def test_update_write_permission_failure_is_not_reported_as_busy(self) -> None:
        self.install(self.codex)
        error = PermissionError(errno.EPERM, "Operation not permitted")
        with (
            patch("asx_runtime.file_lock", side_effect=error),
            patch.object(updates, "_fetch", side_effect=AssertionError("network forbidden")),
            self.assertRaises(AsxError) as caught,
        ):
            updates.update_apply()
        self.assertEqual(caught.exception.code, "update_failed")
        self.assertEqual(read_version(self.codex), "0.4.0")

    def test_entrypoint_returns_utf8_even_with_ascii_stdio_environment(self) -> None:
        self.install(self.codex)
        proc = subprocess.run(
            [
                sys.executable,
                str(self.codex / "scripts" / "asynx.py"),
                "image",
                "info",
                str(self.root / "missing.png"),
            ],
            env={**self.env, "PYTHONIOENCODING": "ascii"},
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(proc.returncode, 2, proc.stderr)
        payload = json.loads(proc.stdout.decode("utf-8"))
        self.assertTrue(any(ord(c) > 127 for c in payload["error"]["message"]))

    def test_status_and_check_cli_do_not_require_api_client(self) -> None:
        with patch.object(
            cli, "_client", side_effect=AssertionError("credentials forbidden")
        ):
            payload, code = cli.execute(cli.parser().parse_args(["update", "status"]))
            self.assertEqual(code, 0)
            self.assertIn("installed_copies", payload)
            with patch.object(updates, "_fetch", side_effect=self.fetch):
                payload, code = cli.execute(
                    cli.parser().parse_args(["update", "check"])
                )
                self.assertEqual(code, 0)

    def test_no_notice_before_submit_or_for_local_processing(self) -> None:
        cases = [
            (
                ["generate", "--prompt", "test", "--detach"],
                {"ok": True, "status": "queued"},
            ),
            (["image", "info", "local.png"], {"ok": True, "image": {}}),
            (["generate", "--prompt", "test"], {"ok": False, "status": "failed"}),
        ]
        for args, payload in cases:
            with (
                self.subTest(args=args),
                patch.object(sys, "argv", ["asynx.py", *args]),
                patch.object(cli, "execute", return_value=(payload, 0)),
                patch.object(cli, "emit"),
                patch.object(updates, "automatic_update_notice") as notice,
            ):
                with self.assertRaises(SystemExit):
                    cli.main()
                notice.assert_not_called()

    def test_successful_task_receives_notice_after_execution(self) -> None:
        events: list[str] = []

        def execute(*_args: Any, **_kwargs: Any) -> tuple[dict[str, Any], int]:
            events.append("executed")
            return {"ok": True, "status": "succeeded", "files": ["image.png"]}, 0

        def notice() -> dict[str, str]:
            events.append("checked")
            return {"latest_version": "0.6.0"}

        with (
            patch.object(sys, "argv", ["asynx.py", "wait", "task_test"]),
            patch.object(cli, "execute", side_effect=execute),
            patch.object(cli, "emit") as emit,
            patch.object(updates, "automatic_update_notice", side_effect=notice),
            self.assertRaises(SystemExit),
        ):
            cli.main()
        self.assertEqual(events, ["executed", "checked"])
        self.assertIn("update_notice", emit.call_args.args[0])


class PackageTestCase(unittest.TestCase):
    def test_numeric_versions_and_invalid_values(self) -> None:
        self.assertGreater(version_tuple("0.10.0"), version_tuple("0.9.9"))
        for version in ("v0.5.0", "0.5.0-beta", "0.05.0", "main", "1;rm -rf"):
            with self.subTest(version=version), self.assertRaises(AsxError):
                version_tuple(version)

    def test_archive_rejects_traversal_links_and_collisions(self) -> None:
        for name, mode in (
            ("../escaped", 0o100644),
            ("/asx/SKILL.md", 0o100644),
            ("asx/../../escaped", 0o100644),
            ("asx/scripts/evil", stat.S_IFLNK | 0o777),
            ("asx/scripts/C:evil", 0o100644),
            ("asx/scripts/..\\evil", 0o100644),
            ("asx/.asx-install.json", 0o100644),
            ("asx/scripts/vendor/evil.py", 0o100644),
            ("asx/scripts/CON.py", 0o100644),
            ("asx/scripts/trailing. ", 0o100644),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                archive = root / "archive.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    item = zipfile.ZipInfo(name)
                    item.external_attr = mode << 16
                    output.writestr(item, "invalid")
                manifest = {
                    "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                    "version": "0.6.0",
                }
                with self.assertRaises(AsxError) as error:
                    extract_package(archive, root / "out", manifest)
                self.assertEqual(error.exception.code, "invalid_update_package")
                self.assertFalse((root / "escaped").exists())

    def test_manifest_rejects_wrong_version_archive_and_checksum(self) -> None:
        good = {
            "schema": 1,
            "version": "0.5.0",
            "archive": "asx-0.5.0.zip",
            "python_min": "3.10",
            "sha256": "a" * 64,
        }
        for field, value in (
            ("version", "0.6.0"),
            ("archive", "../../evil.zip"),
            ("sha256", "bad"),
            ("python_min", "x"),
        ):
            with self.subTest(field=field), self.assertRaises(AsxError):
                validate_manifest({**good, field: value}, "0.5.0")

    def test_package_version_and_uncompressed_size_are_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            UpdateTestCase.copy_skill(source, "0.6.0")
            archive, manifest_file = build_release(source, root / "build")
            manifest = json.loads(manifest_file.read_text())
            with self.assertRaises(AsxError):
                extract_package(
                    archive, root / "wrong-version", {**manifest, "version": "0.7.0"}
                )
            with (
                patch("asxlib.update_files.MAX_UNPACKED_BYTES", 1),
                self.assertRaises(AsxError),
            ):
                extract_package(archive, root / "oversized", manifest)

    def test_duplicate_and_case_colliding_paths_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "duplicate.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("asx/SKILL.md", "one")
                package.writestr("asx/skill.md", "two")
            with self.assertRaises(AsxError):
                extract_package(
                    archive,
                    root / "out",
                    {
                        "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                        "version": "0.6.0",
                    },
                )

    def test_release_build_is_reproducible_and_excludes_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            UpdateTestCase.copy_skill(source, "0.5.0")
            (source / "private.json").write_text("secret")
            vendor = source / "scripts" / "vendor"
            vendor.mkdir()
            (vendor / "secret.py").write_text("not distributable")
            first, manifest = build_release(source, root / "a")
            second, _other = build_release(source, root / "b")
            self.assertEqual(first.read_bytes(), second.read_bytes())
            data = json.loads(manifest.read_text())
            unpacked = extract_package(first, root / "unpacked", data)
            self.assertEqual(read_version(unpacked), "0.5.0")
            self.assertFalse((unpacked / "private.json").exists())
            self.assertFalse((unpacked / "scripts" / "vendor").exists())

    def test_dependency_installer_failure_does_not_run_staged_code(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("asxlib.update_files.subprocess.run") as runner,
        ):
            runner.return_value.returncode = 1
            with self.assertRaises(AsxError) as error:
                prepare_dependencies(Path(directory) / "new", Path(directory) / "old")
            self.assertEqual(error.exception.code, "update_dependencies_failed")
            self.assertEqual(runner.call_count, 1)


class UpdateHTTPTestCase(unittest.TestCase):
    def test_request_sends_no_asynx_or_github_credentials(self) -> None:
        received: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:
                received.update(self.headers.items())
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with (
                patch.dict(
                    os.environ,
                    {"ASYNX_API_KEY": "asx-secret", "GH_TOKEN": "github-secret"},
                ),
                patch.object(updates, "_validate_download_url"),
            ):
                result = updates._fetch(
                    f"http://127.0.0.1:{server.server_port}/", limit=16, timeout=2
                )
            self.assertEqual(result, b"{}")
            self.assertNotIn("Authorization", received)
            self.assertNotIn("secret", json.dumps(received))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_redirect_does_not_allow_untrusted_hosts_or_http(self) -> None:
        for url in (
            "https://evil.example/zip",
            "http://github.com/zip",
            "https://github.com@evil.example/zip",
            "https://github.com:444/zip",
        ):
            with self.subTest(url=url), self.assertRaises(AsxError):
                updates._validate_download_url(url)

    def test_response_limit_and_http_error_close_handles(self) -> None:
        class Response(io.BytesIO):
            headers: ClassVar[dict[str, str]] = {"Content-Length": "20"}

        response = Response(b"x" * 20)
        with patch.object(updates, "build_opener") as factory:
            factory.return_value.open.return_value = response
            with self.assertRaises(AsxError) as caught:
                updates._fetch(updates.LATEST_RELEASE_URL, limit=10, timeout=2)
        self.assertEqual(caught.exception.code, "update_response_too_large")
        self.assertTrue(response.closed)
        body = io.BytesIO(b"no release")
        error = HTTPError(updates.LATEST_RELEASE_URL, 404, "missing", {}, body)  # type: ignore[arg-type]
        with patch.object(updates, "build_opener") as factory:
            factory.return_value.open.side_effect = error
            with self.assertRaises(AsxError) as caught:
                updates._fetch(updates.LATEST_RELEASE_URL, limit=10, timeout=2)
        self.assertEqual(caught.exception.code, "no_stable_release")
        self.assertTrue(body.closed)


if __name__ == "__main__":
    unittest.main()
