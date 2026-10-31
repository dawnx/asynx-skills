from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills" / "asynx-image" / "scripts"))

from asxlib.errors import AsxError

import install as installer


class InstallerMessageTests(unittest.TestCase):
    def test_invalid_arguments_do_not_echo_values(self) -> None:
        output = io.StringIO()
        with patch.object(sys, "argv", ["install.py", "--unknown", "private-value"]), \
             contextlib.redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            installer.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("private-value", output.getvalue())
        self.assertIn("--help", output.getvalue())

    def test_system_and_unexpected_failures_do_not_expose_exception_text(self) -> None:
        for error in (
            PermissionError("private-path-and-secret"),
            FileNotFoundError("private-path-and-secret"),
            ValueError("private-path-and-secret"),
        ):
            with self.subTest(error=type(error).__name__):
                output = io.StringIO()
                with patch.object(installer, "run", side_effect=error), contextlib.redirect_stderr(output):
                    result = installer.main()
                self.assertEqual(result, 1)
                self.assertNotIn("private-path-and-secret", output.getvalue())
                self.assertNotIn("Traceback", output.getvalue())
                self.assertTrue(output.getvalue().strip())

    def test_known_failure_preserves_actionable_message_and_exit_status(self) -> None:
        error = AsxError("请检查安装目录权限。", code="installation_error", exit_code=3)
        output = io.StringIO()
        with patch.object(installer, "run", side_effect=error), contextlib.redirect_stderr(output):
            result = installer.main()
        self.assertEqual(result, 3)
        self.assertIn(error.message, output.getvalue())


if __name__ == "__main__":
    unittest.main()
