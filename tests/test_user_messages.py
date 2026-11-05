from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/asynx-image/scripts"))

from asxlib import cli, config, tasks
from asxlib.client import _api_error
from asxlib.errors import AsxError, friendly_task_error

import install as installer


class UserMessageTests(unittest.TestCase):
    def test_api_errors_preserve_identity_without_echoing_remote_body(self):
        for code, status in (("invalid_api_key", 401), ("unexpected_service_error", 502), ("busy", 429)):
            with self.subTest(code=code):
                error = _api_error(status, json.dumps({
                    "code": code, "message": "private-service-body-and-key",
                    "request_id": "req_test", "data": {"task_id": "task_test"},
                }).encode(), {})
                payload = error.payload()
                self.assertEqual(payload["error"]["code"], code)
                self.assertEqual(payload["error"]["http_status"], status)
                self.assertEqual(payload["request_id"], "req_test")
                self.assertEqual(payload["task_id"], "task_test")
                self.assertNotIn("private-service-body-and-key", json.dumps(payload))
                self.assertRegex(payload["error"]["message"], r"[\u4e00-\u9fff]")

    def test_model_unavailable_message_hides_configuration_without_inventing_recovery(self):
        error = _api_error(503, json.dumps({"code": "group_model_unavailable", "data": {"task_id": None}}).encode(), {})
        self.assertIn("模型暂不可用", error.message)
        self.assertNotRegex(error.message, "分组|渠道|路由|配置")
        self.assertTrue(error.submission_rejected)
        error.task_id = "task_existing"
        self.assertFalse(error.submission_rejected)

    def test_result_error_does_not_expose_network_details_or_claim_success(self):
        for code in ("provider_result_unavailable", "invalid_provider_result"):
            with self.subTest(code=code):
                result = friendly_task_error({"code": code, "message": "unsafe result URL 198.18.0.14 SSRF"})
                self.assertEqual(result["code"], code)
                self.assertNotRegex(result["message"], "198\\.18|SSRF|unsafe|已生成")
                self.assertIn("避免重复生成", result["message"])

    def test_task_error_keeps_parameter_and_phase_without_echoing_values(self):
        remote = {
            "code": "provider_request_rejected", "phase": "submit",
            "message": "Invalid value for quality: 'private-value'. Supported values are: 'auto'.",
        }
        result = tasks.task_payload({"id": "task_test", "status": "failed", "error": remote}, "req_test")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], remote["code"])
        self.assertEqual(result["error"]["phase"], "submit")
        self.assertIn("quality", result["error"]["message"])
        self.assertNotIn("private-value", result["error"]["message"])
        self.assertIn("private-value", remote["message"])

    def test_terminal_error_without_details_does_not_invent_a_cause(self):
        for status in ("failed", "timeout", "canceled"):
            error = friendly_task_error(None, status)
            self.assertEqual(error["code"], f"task_{status}")
            self.assertRegex(error["message"], r"[\u4e00-\u9fff]")
        unknown = friendly_task_error({"code": "unknown", "message": "secret-provider-error"}, "failed")
        self.assertNotIn("secret-provider-error", unknown["message"])
        self.assertIn("未提供", unknown["message"])

    def test_old_local_remote_error_is_readable_but_download_guidance_is_preserved(self):
        remote_error = {"code": "task_failed", "message": "private-service-message"}
        row = {"local_id": "local_test", "remote_task_id": "task_test", "status": "failed",
               "error": remote_error, "remote": {"error": remote_error}}
        payload = tasks._local_task_payload(row)
        self.assertNotIn("private-service-message", payload["error"]["message"])
        local_error = {"code": "output_write_failed", "message": "检查保存目录，然后下载原任务。"}
        row.update(status="succeeded", error=local_error)
        self.assertEqual(tasks._local_task_payload(row)["error"], local_error)

    def test_history_localizes_errors_without_mutating_remote_records(self):
        records = [{"id": "task_test", "status": "failed", "error": {
            "code": "task_failed", "message": "private-service-message",
        }}]
        client = Mock()
        client.list_tasks.return_value = ({"items": records}, "req_test")
        result = tasks.history_payload(client, status=None, model=None, limit=20)
        self.assertNotIn("private-service-message", result["tasks"][0]["error"]["message"])
        self.assertEqual(records[0]["error"]["message"], "private-service-message")
        client.submit.assert_not_called()

    def test_invalid_cli_arguments_return_readable_error_before_execution(self):
        for args in (["generate"], ["video", "generate", "--prompt", "test", "--duration", "private-value"],
                     ["generate", "--prompt", "test", "--unknown", "private-value"]):
            output = io.StringIO()
            with patch.object(sys, "argv", ["asynx.py", *args]), patch.object(cli, "execute") as execute, \
                 patch.object(cli, "legacy_installation_notice", return_value=None), contextlib.redirect_stdout(output), \
                 self.assertRaises(SystemExit) as raised:
                cli.main()
            self.assertEqual(raised.exception.code, 2)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["error"]["code"], "invalid_arguments")
            self.assertIn("--help", payload["error"]["message"])
            self.assertNotIn("private-value", payload["error"]["message"])
            execute.assert_not_called()

    def test_unexpected_cli_failure_does_not_echo_exception_body(self):
        output = io.StringIO()
        with patch.object(sys, "argv", ["asynx.py", "recent"]), \
             patch.object(cli, "execute", side_effect=ValueError("private-exception-body")), \
             patch.object(cli, "legacy_installation_notice", return_value=None), contextlib.redirect_stdout(output), \
             self.assertRaises(SystemExit) as raised:
            cli.main()
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn("private-exception-body", output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["error"]["code"], "unexpected_error")

    def test_video_only_catalog_passes_installer_and_doctor_verification(self):
        api = Mock()
        api.models.return_value = ([{"name": "video-model", "task_types": ["video.generate"]}], "req_test")
        client = SimpleNamespace(AsxError=AsxError, load_credentials=lambda: ("test-key", "https://example.test"),
                                 AsynxClient=Mock(return_value=api))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(installer._verify(client), 0)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {key: value for key, value in os.environ.items() if not key.startswith("ASYNX_")}
            environment["ASYNX_CONFIG_PATH"] = str(root / "config.json")
            with patch.dict(os.environ, environment, clear=True), patch("asxlib.client.AsynxClient", return_value=api):
                config.save_config("test-key", "https://example.test")
                result = config.doctor(verify=True)
            self.assertTrue(result["verification"]["ok"])
            self.assertEqual(result["verification"]["image_model_count"], 0)
            self.assertEqual(result["verification"]["video_model_count"], 1)
        api.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
