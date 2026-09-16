from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "asx" / "scripts"
CLI = SCRIPTS / "asynx.py"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from asxlib import config

import install as installer


class TTY:
    @staticmethod
    def isatty() -> bool:
        return True


def isolated_environment(home: Path, **values: str) -> dict[str, str]:
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("ASYNX_") or name in {"HOME", "XDG_CONFIG_HOME"}:
            environment.pop(name, None)
    environment["HOME"] = str(home)
    environment.update(values)
    return environment


def run_cli(
    script: Path,
    environment: dict[str, str],
    *arguments: str,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    result = subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            f"CLI stdout is not JSON: {result.stdout!r}; stderr: {result.stderr!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise TypeError(f"CLI payload is not an object: {payload!r}")
    return result, cast(dict[str, Any], payload)


class ConfigurationPathTestCase(unittest.TestCase):
    def test_explicit_config_path_remains_highest_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            explicit = root / "profiles" / "work.json"
            with patch.dict(
                os.environ,
                isolated_environment(
                    root / "home",
                    ASYNX_CONFIG_PATH=str(explicit),
                    XDG_CONFIG_HOME=str(root / "xdg"),
                ),
                clear=True,
            ):
                self.assertEqual(config.config_path(), explicit)

    def test_canonical_path_ignores_xdg_config_home(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX canonical path behavior")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            xdg_home = root / "xdg"
            with patch.dict(
                os.environ,
                isolated_environment(home, XDG_CONFIG_HOME=str(xdg_home)),
                clear=True,
            ):
                self.assertEqual(config.config_path(), home / ".config" / "asynx" / "config.json")

    def test_read_config_falls_back_to_legacy_xdg_path(self) -> None:
        if os.name == "nt":
            self.skipTest("XDG legacy migration is POSIX-only")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            legacy = root / "old-xdg" / "asynx" / "config.json"
            legacy.parent.mkdir(parents=True)
            expected = {
                "api_key": "asx-legacy-key",
                "base_url": "https://asynx.llmapi.site/api",
            }
            legacy.write_text(json.dumps(expected), encoding="utf-8")
            with patch.dict(
                os.environ,
                isolated_environment(home, XDG_CONFIG_HOME=str(legacy.parents[1])),
                clear=True,
            ):
                actual, source = config.read_config_with_source()
            self.assertEqual(actual, expected)
            self.assertEqual(source, legacy)

    def test_canonical_config_takes_precedence_over_legacy_xdg_file(self) -> None:
        if os.name == "nt":
            self.skipTest("XDG legacy migration is POSIX-only")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            canonical = home / ".config" / "asynx" / "config.json"
            legacy = root / "old-xdg" / "asynx" / "config.json"
            canonical.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            canonical.write_text(json.dumps({"api_key": "asx-canonical"}), encoding="utf-8")
            legacy.write_text(json.dumps({"api_key": "asx-legacy"}), encoding="utf-8")
            with patch.dict(
                os.environ,
                isolated_environment(home, XDG_CONFIG_HOME=str(legacy.parents[1])),
                clear=True,
            ):
                actual, source = config.read_config_with_source()
                migrated = config.migrate_legacy_config()

            self.assertEqual(actual["api_key"], "asx-canonical")
            self.assertEqual(source, canonical)
            self.assertIsNone(migrated)
            self.assertEqual(
                json.loads(canonical.read_text(encoding="utf-8"))["api_key"],
                "asx-canonical",
            )

    def test_migrate_legacy_config_creates_private_canonical_copy(self) -> None:
        if os.name == "nt":
            self.skipTest("XDG legacy migration is POSIX-only")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            legacy = root / "old-xdg" / "asynx" / "config.json"
            canonical = home / ".config" / "asynx" / "config.json"
            legacy.parent.mkdir(parents=True)
            expected = {
                "api_key": "asx-legacy-key",
                "base_url": "https://asynx.llmapi.site/api",
            }
            legacy.write_text(json.dumps(expected), encoding="utf-8")
            with patch.dict(
                os.environ,
                isolated_environment(home, XDG_CONFIG_HOME=str(legacy.parents[1])),
                clear=True,
            ):
                migrated = config.migrate_legacy_config()

            self.assertEqual(migrated, canonical)
            self.assertTrue(legacy.exists(), "migration must leave the legacy file intact")
            self.assertEqual(json.loads(canonical.read_text(encoding="utf-8")), expected)
            self.assertEqual(stat.S_IMODE(canonical.stat().st_mode), 0o600)

    def test_configure_writes_canonical_path_even_with_xdg_override(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX canonical path behavior")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            xdg_home = root / "xdg"
            canonical = home / ".config" / "asynx" / "config.json"
            with (
                patch.dict(
                    os.environ,
                    isolated_environment(home, XDG_CONFIG_HOME=str(xdg_home)),
                    clear=True,
                ),
                patch("asxlib.config.sys.stdin", TTY()),
                patch("asxlib.config.getpass.getpass", return_value="asx-configured-key"),
            ):
                result = config.configure()

            self.assertEqual(result["config_path"], str(canonical.resolve()))
            self.assertTrue(canonical.is_file())
            self.assertFalse((xdg_home / "asynx" / "config.json").exists())

    def test_installer_migrates_legacy_config_even_when_configuration_is_skipped(self) -> None:
        if os.name == "nt":
            self.skipTest("XDG legacy migration is POSIX-only")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            legacy = root / "old-xdg" / "asynx" / "config.json"
            canonical = home / ".config" / "asynx" / "config.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(
                json.dumps(
                    {
                        "api_key": "asx-installer-migration",
                        "base_url": "https://asynx.llmapi.site/api",
                    }
                ),
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                isolated_environment(home, XDG_CONFIG_HOME=str(legacy.parents[1])),
                clear=True,
            ):
                result = installer.run(
                    ["--target", "codex", "--skip-config", "--no-verify"],
                    home=home,
                    install_dependencies=False,
                )

            self.assertEqual(result, 0)
            self.assertTrue(canonical.is_file())
            self.assertTrue(legacy.is_file())


class ConfigurationCLITestCase(unittest.TestCase):
    def test_new_process_and_installed_copies_share_saved_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex = installer._target_path("codex", home)
            claude = installer._target_path("claude", home)
            installer._install_skill(codex, install_dependencies=False)
            installer._install_skill(claude, install_dependencies=False)
            environment = isolated_environment(home)
            secret = "asx-persisted-secret-123456789"

            writer = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import sys;"
                        f"sys.path.insert(0, {str(SCRIPTS)!r});"
                        "from asxlib.config import save_config;"
                        f"save_config({secret!r}, 'https://asynx.llmapi.site/api')"
                    ),
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(writer.returncode, 0, writer.stderr)

            statuses: list[dict[str, Any]] = []
            for installed in (codex, claude):
                result, payload = run_cli(
                    installed / "scripts" / "asynx.py",
                    environment,
                    "config",
                    "status",
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                statuses.append(payload)

            canonical = (home / ".config" / "asynx" / "config.json").resolve()
            self.assertEqual(
                {Path(cast(str, item["config_path"])).resolve() for item in statuses},
                {canonical},
            )
            self.assertTrue(all(item["configured"] for item in statuses))
            self.assertTrue(all(item["credential_source"] == "config_file" for item in statuses))
            self.assertTrue(
                all(
                    Path(cast(str, item["loaded_config_path"])).resolve() == canonical
                    for item in statuses
                )
            )
            self.assertNotIn(secret, json.dumps(statuses))

    def test_config_status_reports_environment_source_without_leaking_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            secret = "asx-environment-secret-123456789"
            environment = isolated_environment(home, ASYNX_API_KEY=secret)
            result, payload = run_cli(CLI, environment, "config", "status")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(payload["configured"])
            self.assertEqual(payload["credential_source"], "environment")
            self.assertIn("ASYNX_API_KEY", payload["environment_overrides"])
            self.assertFalse(payload["config_exists"])
            self.assertNotIn(secret, json.dumps(payload))

    def test_config_status_does_not_echo_credentials_from_invalid_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            secret = "base-url-password"
            environment = isolated_environment(
                home,
                ASYNX_API_KEY="asx-status-test",
                ASYNX_BASE_URL=f"https://user:{secret}@example.com/api",
            )
            result, payload = run_cli(CLI, environment, "config", "status")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(payload["base_url_valid"])
            self.assertEqual(payload["base_url"], "<invalid>")
            self.assertNotIn(secret, json.dumps(payload))

    def test_missing_key_error_includes_checked_path_and_absolute_configure_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            environment = isolated_environment(home)
            result, payload = run_cli(CLI, environment, "models")

            self.assertEqual(result.returncode, 2, result.stderr)
            error = cast(dict[str, Any], payload["error"])
            details = cast(dict[str, Any], error["details"])
            canonical = (home / ".config" / "asynx" / "config.json").resolve()
            self.assertEqual(error["code"], "missing_api_key")
            self.assertEqual(details["credential_source"], "none")
            self.assertEqual(Path(details["config_path"]).resolve(), canonical)
            self.assertIn(canonical, {Path(item).resolve() for item in details["checked_paths"]})
            self.assertIn(str(CLI.resolve()), details["configure_command"])
            self.assertIn(details["config_path"], error["message"])
            self.assertIn(details["configure_command"], error["message"])

    def test_noninteractive_configure_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, payload = run_cli(CLI, isolated_environment(Path(directory)), "configure")

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(payload["error"]["code"], "interactive_terminal_required")

    def test_doctor_detects_installed_version_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            codex = installer._target_path("codex", home)
            claude = installer._target_path("claude", home)
            installer._install_skill(codex, install_dependencies=False)
            installer._install_skill(claude, install_dependencies=False)
            constants = claude / "scripts" / "asxlib" / "constants.py"
            original = constants.read_text(encoding="utf-8")
            changed = re.sub(
                r'^VERSION = "[^"]+"$',
                'VERSION = "0.0.0-test"',
                original,
                count=1,
                flags=re.MULTILINE,
            )
            self.assertNotEqual(changed, original)
            constants.write_text(changed, encoding="utf-8")

            result, payload = run_cli(
                CLI,
                isolated_environment(home, ASYNX_API_KEY="asx-doctor-test"),
                "doctor",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            installations = cast(list[dict[str, Any]], payload["installations"])
            by_path = {item["path"]: item for item in installations}
            self.assertEqual(by_path[str(codex.resolve())]["version"], payload["skill_version"])
            self.assertEqual(by_path[str(claude.resolve())]["version"], "0.0.0-test")
            issues = cast(list[dict[str, Any]], payload["issues"])
            self.assertTrue(
                any(
                    "version" in str(issue.get("code", "")).casefold()
                    or "version" in str(issue.get("message", "")).casefold()
                    for issue in issues
                ),
                issues,
            )

    def test_doctor_warns_when_canonical_and_legacy_configs_both_exist(self) -> None:
        if os.name == "nt":
            self.skipTest("XDG legacy migration is POSIX-only")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "home"
            canonical = home / ".config" / "asynx" / "config.json"
            legacy = root / "old-xdg" / "asynx" / "config.json"
            canonical.parent.mkdir(parents=True)
            legacy.parent.mkdir(parents=True)
            canonical.write_text(json.dumps({"api_key": "asx-canonical"}), encoding="utf-8")
            legacy.write_text(json.dumps({"api_key": "asx-legacy"}), encoding="utf-8")
            environment = isolated_environment(
                home,
                ASYNX_API_KEY="asx-doctor-test",
                XDG_CONFIG_HOME=str(legacy.parents[1]),
            )

            result, payload = run_cli(CLI, environment, "doctor")

            self.assertEqual(result.returncode, 0, result.stderr)
            issues = cast(list[dict[str, Any]], payload["issues"])
            self.assertTrue(
                any(issue.get("code") == "multiple_config_files" for issue in issues),
                issues,
            )
            checks = cast(list[dict[str, Any]], payload["checks"])
            config_check = next(item for item in checks if item["name"] == "config_files")
            details = cast(dict[str, Any], config_check["details"])
            self.assertEqual(
                set(cast(list[str], details["paths"])),
                {str(canonical), str(legacy)},
            )


if __name__ == "__main__":
    unittest.main()
