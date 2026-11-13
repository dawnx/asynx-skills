from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "asynx-image" / "scripts"
CLI = SCRIPTS / "asynx.py"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from asxlib import cli, config
from asxlib.errors import AsxError


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
        cwd=environment["HOME"],
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
            self.assertEqual(stat.S_IMODE(canonical.stat().st_mode), 0o600)


class ConfigurationCLITestCase(unittest.TestCase):
    def test_configure_terminal_and_redirect_preserve_saved_key_without_exposing_it(self) -> None:
        for terminal in (True, False):
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                output, errors = io.StringIO(), io.StringIO()
                secret = "asx-new-private-key"
                environment_key = "asx-environment-private-key"
                with (
                    patch.dict(os.environ, isolated_environment(home, ASYNX_API_KEY=environment_key), clear=True),
                    patch.object(sys, "argv", [str(CLI), "configure"]),
                    patch.object(sys, "stdin", TTY()),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                    patch.object(output, "isatty", return_value=terminal),
                    patch("asxlib.config.getpass.getpass", return_value=secret),
                    patch.object(cli, "AsynxClient") as api,
                    self.assertRaises(SystemExit) as raised,
                ):
                    cli.main()
                self.assertEqual(raised.exception.code, 0)
                api.assert_not_called()
                with patch.dict(os.environ, isolated_environment(home), clear=True):
                    self.assertEqual(config.read_config()["api_key"], secret)
                if terminal:
                    self.assertEqual(output.getvalue(), "")
                else:
                    payload = json.loads(output.getvalue())
                    self.assertTrue(payload["ok"])
                    self.assertTrue(payload["configured"])
                self.assertIn("ASYNX_API_KEY", errors.getvalue())
                for key in (secret, environment_key):
                    self.assertNotIn(key, output.getvalue() + errors.getvalue())

    def test_configure_cancel_preserves_existing_key_and_exit_status(self) -> None:
        for interruption in (EOFError, KeyboardInterrupt):
            with self.subTest(interruption=interruption), tempfile.TemporaryDirectory() as directory:
                home = Path(directory)
                output, errors = io.StringIO(), io.StringIO()
                with (
                    patch.dict(os.environ, isolated_environment(home), clear=True),
                    patch.object(sys, "argv", [str(CLI), "configure"]),
                    patch.object(sys, "stdin", TTY()),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(errors),
                    patch.object(output, "isatty", return_value=True),
                    patch("asxlib.config.getpass.getpass", side_effect=interruption),
                ):
                    path = config.save_config("asx-existing-private-key", config.DEFAULT_BASE_URL)
                    before = path.read_bytes()
                    with self.assertRaises(SystemExit) as raised:
                        cli.main()
                    self.assertEqual(raised.exception.code, 130)
                    self.assertEqual(path.read_bytes(), before)
                self.assertEqual(output.getvalue(), "")
                self.assertNotIn("asx-existing-private-key", errors.getvalue())

    def test_doctor_reports_a_failed_connection_check_without_the_key(self) -> None:
        # A failed check must name the cause and the request, never the credential.
        from unittest.mock import Mock

        secret = "asx-private-key-123456789"
        for code, status in (("network_error", None), ("unauthorized", 401)):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                api = Mock()
                api.models.side_effect = AsxError(
                    "连接检查未通过。", code=code, http_status=status, request_id="req-test"
                )
                environment = isolated_environment(
                    Path(directory),
                    ASYNX_API_KEY=secret,
                    ASYNX_CONFIG_PATH=str(Path(directory) / "config.json"),
                )
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("asxlib.client.AsynxClient", return_value=api),
                ):
                    result = config.doctor(verify=True)
                self.assertFalse(result["ok"])
                self.assertEqual(result["verification"]["error"]["code"], code)
                self.assertIn("api_verification_failed", [item["code"] for item in result["issues"]])
                self.assertNotIn(secret, json.dumps(result))

    def test_new_process_and_separate_copies_share_saved_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            # Two channels can each hold a copy; both read the one user-level file.
            copies = [home / name / "asynx-image" for name in ("first", "second")]
            for copy in copies:
                shutil.copytree(
                    SCRIPTS.parent, copy, ignore=shutil.ignore_patterns("__pycache__")
                )
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
                cwd=home,
                env=environment,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            self.assertEqual(writer.returncode, 0, writer.stderr)

            statuses: list[dict[str, Any]] = []
            for installed in copies:
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
            secret = "sk-environment-secret-123456789"
            environment = isolated_environment(home, ASYNX_API_KEY=secret)
            result, payload = run_cli(CLI, environment, "config", "status")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(payload["configured"])
            self.assertEqual(payload["credential_source"], "environment")
            self.assertTrue(payload["key_valid"])
            self.assertIn("ASYNX_API_KEY", payload["environment_overrides"])
            self.assertFalse(payload["config_exists"])
            self.assertNotIn(secret, json.dumps(payload))

    def test_doctor_accepts_environment_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, payload = run_cli(
                CLI,
                isolated_environment(Path(directory), ASYNX_API_KEY="sk-doctor-test"),
                "doctor",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(payload["config"]["key_valid"])
            credentials = next(item for item in payload["checks"] if item["name"] == "credentials")
            self.assertTrue(credentials["ok"])

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

    def test_missing_key_error_includes_config_path_and_absolute_configure_command(self) -> None:
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
            self.assertIn(str(CLI.resolve()), details["configure_command"])
            self.assertIn(details["config_path"], error["message"])
            self.assertIn(details["configure_command"], error["message"])

    def test_noninteractive_configure_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result, payload = run_cli(CLI, isolated_environment(Path(directory)), "configure")

            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(payload["error"]["code"], "interactive_terminal_required")



if __name__ == "__main__":
    unittest.main()
