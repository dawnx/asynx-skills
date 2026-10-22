from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import config, tasks
from asxlib.errors import AsxError
from test_audit_regressions import FakeClient


class PollingConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config_path = self.root / "config.json"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("ASYNX_")}
        environment.update({
            "ASYNX_CONFIG_PATH": str(self.config_path),
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CACHE_PATH": str(self.root / "cache.json"),
            "ASYNX_UPDATE_CHECK": "off",
        })
        env = patch.dict(os.environ, environment, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = FakeClient()
        self.delays = []
        original_wait = tasks.wait_for_terminal
        waiter = patch.object(
            tasks, "wait_for_terminal",
            side_effect=lambda *args, **kwargs: original_wait(*args, sleep=self.delays.append, **kwargs),
        )
        waiter.start()
        self.addCleanup(waiter.stop)

    def args(self, **overrides):
        values = {
            "prompt": "cat", "model": None, "image_size": None, "aspect_ratio": None, "quality": None,
            "count": 1, "output_format": None, "reference": [], "image": [], "mask": None,
            "keep_reference_original": False, "idempotency_key": "asx-polling-test",
            "output_dir": str(self.root / "outputs"), "detach": False, "from_task": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def set_config(self, delay):
        self.config_path.write_text(json.dumps({"initial_poll_delay_seconds": delay}))

    def test_new_submission_waits_twenty_seconds_by_default(self):
        result, code = tasks.run_submission(self.client, self.args(), "image.generate")
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.delays, [20.0])
        self.assertEqual(len(self.client.submitted), 1)

    def test_edit_uses_persistent_configured_delay(self):
        self.set_config(30)
        source = self.root / "source.png"
        source.write_bytes(self.client.png)
        result, code = tasks.run_submission(self.client, self.args(image=[str(source)]), "image.edit")
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.delays, [30.0])

    def test_environment_override_accepts_zero(self):
        self.set_config(30)
        with patch.dict(os.environ, {"ASYNX_INITIAL_POLL_DELAY_SECONDS": "0"}):
            tasks.run_submission(self.client, self.args(), "image.generate")
        self.assertEqual(self.delays, [0.0])

    def test_invalid_delay_is_rejected_before_submission(self):
        for value in (-1, 301, True, None, [], "bad", "nan", "inf"):
            with self.subTest(value=value):
                self.set_config(value)
                with self.assertRaises(AsxError) as caught:
                    tasks.run_submission(self.client, self.args(), "image.generate")
                self.assertEqual(caught.exception.code, "invalid_poll_delay")
        self.assertEqual(self.client.submitted, [])

    def test_saving_credentials_preserves_polling_preference(self):
        self.set_config(30)
        config.save_config("asx-test", "https://example.test")
        self.assertEqual(config.initial_poll_delay_seconds(), 30)
        self.assertEqual(config.read_config()["api_key"], "asx-test")

    def test_detached_submission_does_not_wait_or_require_polling_config(self):
        self.set_config("invalid")
        result, code = tasks.run_submission(self.client, self.args(detach=True), "image.generate")
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "queued")
        self.assertEqual(self.delays, [])

    def test_terminal_submission_response_returns_without_delay(self):
        original_submit = self.client.submit

        def submit(body, key):
            task, _request_id = original_submit(body, key)
            return self.client.task(task["id"])

        with patch.object(self.client, "submit", side_effect=submit):
            result, code = tasks.run_submission(self.client, self.args(), "image.generate")
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.delays, [])

    def test_wait_existing_task_checks_immediately_then_uses_short_interval(self):
        submitted, _code = tasks.run_submission(self.client, self.args(detach=True), "image.generate")
        final, request_id = self.client.task(submitted["task_id"])
        responses = [({**final, "status": "running"}, request_id), (final, request_id)]
        with patch.object(self.client, "task", side_effect=responses) as query:
            result, code = tasks.wait_and_download(self.client, submitted["task_id"])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(query.call_count, 2)
        self.assertEqual(self.delays, [4.0])

    def test_reused_submission_does_not_repeat_initial_delay(self):
        submitted, _code = tasks.run_submission(self.client, self.args(detach=True), "image.generate")
        final, request_id = self.client.task(submitted["task_id"])
        responses = [({**final, "status": "running"}, request_id), (final, request_id)]
        with patch.object(self.client, "task", side_effect=responses):
            result, code = tasks.run_submission(self.client, self.args(), "image.generate")
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(self.delays, [4.0])
        self.assertEqual(len(self.client.submitted), 1)


if __name__ == "__main__":
    unittest.main()
