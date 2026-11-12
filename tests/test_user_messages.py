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
from asxlib.errors import AsxError, task_error

import install as installer


def answer(status, code, **data):
    """An error envelope as the service sends it."""
    return _api_error(status, json.dumps({
        "code": code, "message": "developer-text-with-upstream-secret",
        "data": {"task_id": None, "details": None, **data}, "request_id": "req_test",
    }).encode(), {})


class UserMessageTests(unittest.TestCase):
    def test_service_guidance_is_reported_and_its_developer_text_is_not(self):
        # The client used to translate codes itself and went stale with every new one.
        error = answer(
            402, "code_this_client_has_never_seen", task_created=False,
            next_step="fix_account", user_message="额度不足，请调整后再提交。",
        )
        payload = error.payload()
        self.assertEqual(payload["error"]["message"], "额度不足，请调整后再提交。")
        self.assertEqual(payload["error"]["next_step"], "fix_account")
        self.assertIs(payload["error"]["task_created"], False)
        self.assertEqual((payload["error"]["code"], payload["request_id"]), ("code_this_client_has_never_seen", "req_test"))
        self.assertNotIn("upstream-secret", json.dumps(payload, ensure_ascii=False))

    def test_only_the_service_can_say_that_no_task_was_created(self):
        # A wrong "not created" turns one request into two paid tasks.
        self.assertTrue(answer(503, "model_unavailable", task_created=False).submission_rejected)
        for unsettled in (
            answer(503, "model_unavailable"),                       # the service did not say
            answer(500, "internal_error", task_created=None),       # it could not tell
            answer(409, "x", task_created=False, task_id="task_1"),  # a task ID always wins
            _api_error(502, b"<html>bad gateway</html>", {}),       # not the service at all
        ):
            self.assertFalse(unsettled.submission_rejected)
            self.assertNotIn("task_created", unsettled.payload()["error"])
        replayed = answer(401, "invalid_api_key", task_created=False)
        replayed.replayed = True  # an earlier attempt under the same key got no answer
        self.assertFalse(replayed.submission_rejected)

    def test_answers_without_guidance_get_a_generic_message_and_hostile_text_is_contained(self):
        proxy = _api_error(502, b"<html>upstream-secret</html>", {})
        self.assertEqual(proxy.code, "http_502")
        self.assertRegex(proxy.message, r"[\u4e00-\u9fff]")
        self.assertNotIn("upstream-secret", proxy.message)
        self.assertIsNone(proxy.next_step)
        hostile = answer(422, "x", next_step="run_this_command", user_message="行一\x1b[31m" + "长" * 5000)
        self.assertIsNone(hostile.next_step)
        self.assertNotIn("\x1b", hostile.message)
        self.assertLessEqual(len(hostile.message), 1000)
        # File transfers nest the same fields under "error".
        nested = _api_error(410, json.dumps({"error": {
            "code": "asset_expired", "message": "dev", "next_step": "change_request",
            "user_message": "结果文件已超过保留期。",
        }}).encode(), {})
        self.assertEqual((nested.code, nested.message, nested.next_step),
                         ("asset_expired", "结果文件已超过保留期。", "change_request"))

    def test_failed_task_reports_the_service_explanation_never_its_developer_text(self):
        # Administrator wording arrives as user_message; "message" may carry upstream text.
        shown = task_error({
            "code": "task_failed", "message": "upstream said: secret-provider-error",
            "next_step": "change_request", "user_message": "提示词触发了内容策略，请修改后重试。",
            "phase": "running", "details": {"field": "input.quality"},
        })
        self.assertEqual(shown, {
            "code": "task_failed", "message": "提示词触发了内容策略，请修改后重试。",
            "next_step": "change_request", "phase": "running", "details": {"field": "input.quality"},
        })
        bare = task_error({"code": "unknown", "message": "secret-provider-error"}, "failed")
        self.assertNotIn("secret-provider-error", bare["message"])
        self.assertEqual(bare["next_step"], "retry_later")
        for status in ("failed", "timeout", "canceled"):
            self.assertEqual(task_error(None, status)["code"], f"task_{status}")
        terminal = tasks.task_payload({"id": "task_x", "status": "failed", "error": {
            "code": "provider_request_rejected", "message": "private", "user_message": "请检查提示词。",
        }}, None)
        self.assertEqual(terminal["error"]["message"], "请检查提示词。")
        self.assertNotIn("next_action_args", terminal)

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
                     contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
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
             contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
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
