from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "asynx-image" / "scripts"
CLI = SCRIPTS / "asynx.py"
sys.path.insert(0, str(SCRIPTS))

from asxlib import dependencies, local_images, reference_media
from asxlib.constants import VERSION
from asxlib.errors import AsxError


class PluginManifestTests(unittest.TestCase):
    def test_plugin_version_is_the_skill_version(self) -> None:
        # Claude Code hands users a new copy only when this string changes: a
        # release whose manifest still names the old version reaches nobody.
        manifest = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], VERSION)


class SetupTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.secret = "asx-setup-secret-123456789"
        environment = {
            name: value for name, value in os.environ.items() if not name.startswith("ASYNX_")
        }
        environment.update(
            {
                "HOME": str(self.root),
                "ASYNX_STATE_PATH": str(self.root / "state" / "state.db"),
                "ASYNX_CONFIG_PATH": str(self.root / "config.json"),
            }
        )
        for patcher in (
            patch.dict(os.environ, environment, clear=True),
            patch.object(sys, "path", list(sys.path)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.vendor = (
            self.root / "state" / "vendor"
            / f"py{sys.version_info.major}{sys.version_info.minor}"
        )

    def test_missing_pillow_is_installed_outside_the_skill_and_the_key_stays_private(self) -> None:
        # The Skill's own directory is replaced on update, so nothing may be installed into it.
        with (
            patch.dict(os.environ, {"ASYNX_API_KEY": self.secret}),
            patch.object(dependencies, "pillow_version", side_effect=[None, "11.0.0", "11.0.0"]),
            patch.object(dependencies.subprocess, "run") as pip,
        ):
            pip.return_value.returncode = 0
            result = dependencies.setup()

        command = pip.call_args.args[0]
        self.assertEqual(command[:4], [sys.executable, "-m", "pip", "install"])
        self.assertEqual(command[command.index("--target") + 1], str(self.vendor))
        self.assertEqual(command[-1], dependencies.PILLOW_REQUIREMENT)
        self.assertFalse(self.vendor.is_relative_to(SCRIPTS.parent))
        self.assertEqual(result["pillow"]["version"], "11.0.0")
        self.assertTrue(result["pillow"]["installed_now"])
        self.assertTrue(result["configured"])
        self.assertIn(str(CLI), result["configure_command"])
        self.assertNotIn(self.secret, json.dumps(result))

    def test_importable_pillow_is_left_alone_and_a_missing_key_is_reported(self) -> None:
        with (
            patch.object(dependencies, "pillow_version", return_value="10.4.0"),
            patch.object(dependencies.subprocess, "run") as pip,
        ):
            result = dependencies.setup()

        pip.assert_not_called()
        self.assertFalse(self.vendor.exists())
        self.assertFalse(result["pillow"]["installed_now"])
        self.assertFalse(result["configured"])
        self.assertIn(result["configure_command"], result["message"])

    def test_failed_installation_names_the_manual_command_and_hides_pip_output(self) -> None:
        cases = {
            "dependency_install_failed": patch.object(
                dependencies.subprocess, "run", return_value=SimpleNamespace(
                    returncode=1, stderr=b"https://user:index-password@pypi.example/simple"
                ),
            ),
            "pip_not_available": patch.object(
                dependencies.importlib.util, "find_spec", return_value=None
            ),
        }
        for code, failure in cases.items():
            with (
                self.subTest(code=code),
                patch.object(dependencies, "pillow_version", return_value=None),
                failure,
                self.assertRaises(AsxError) as caught,
            ):
                dependencies.setup()
            error = caught.exception
            self.assertEqual(error.code, code)
            self.assertIn(dependencies.PILLOW_REQUIREMENT, error.details["manual_command"])
            self.assertIn(str(self.vendor), error.details["manual_command"])
            self.assertIn(error.details["manual_command"], error.message)
            self.assertNotIn("index-password", json.dumps(error.payload()))

    def test_commands_that_need_pillow_point_to_setup(self) -> None:
        for module in (local_images, reference_media):
            with (
                self.subTest(module=module.__name__),
                patch.object(module.importlib, "import_module", side_effect=ImportError),
                self.assertRaises(AsxError) as caught,
            ):
                module._pillow()
            self.assertEqual(caught.exception.code, "pillow_not_installed")
            self.assertEqual(caught.exception.details["next_action_args"], ["setup"])

    def test_cli_imports_pillow_from_the_per_user_directory(self) -> None:
        # Without this wiring a successful `setup` would still leave every image command failing.
        package = self.root / "vendor" / "PIL"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text('__version__ = "99.0.0"\n', encoding="utf-8")
        (package / "Image.py").write_text("", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(CLI), "setup"],
            cwd=self.root,
            env={**os.environ, "ASYNX_VENDOR_PATH": str(package.parent)},
            stdin=subprocess.DEVNULL,
            text=True, encoding="utf-8",
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["pillow"]["version"], "99.0.0")
        self.assertFalse(payload["pillow"]["installed_now"])
        self.assertFalse(payload["configured"])


if __name__ == "__main__":
    unittest.main()
