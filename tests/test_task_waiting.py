from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import tasks
from asxlib.client import AsynxClient
from asxlib.constants import TASK_WAIT_SECONDS
from test_audit_regressions import FakeClient


class HoldingHandler(BaseHTTPRequestHandler):
    """Answers a task read after holding it, like a service waiting for a change."""

    hold_seconds = 0.5
    queries: ClassVar[list[str]] = []

    def log_message(self, format, *args):
        return

    def do_GET(self):
        type(self).queries.append(urlsplit(self.path).query)
        time.sleep(self.hold_seconds)
        raw = json.dumps({
            "code": "ok", "message": None, "request_id": "req_wait",
            "data": {"id": "task_held", "status": "running"},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class WaitRequestTests(unittest.TestCase):
    def test_held_answer_outlasting_the_request_timeout_is_not_a_network_error(self):
        # A healthy long wait must not hit the socket timeout of an ordinary request.
        HoldingHandler.queries = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), HoldingHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = AsynxClient(f"http://127.0.0.1:{server.server_address[1]}", "asx-test")
        with patch("asxlib.client.REQUEST_TIMEOUT_SECONDS", 0.2):
            task, _request_id = client.task("task_held", wait=1)
        self.assertEqual(task["status"], "running")
        self.assertEqual(HoldingHandler.queries, ["wait=1"])


class ScriptedClient:
    """Answers each read with the next status after the scripted time has passed."""

    def __init__(self, clock, answers):
        self.clock = clock
        self.answers = iter(answers)
        self.waits = []

    def task(self, task_id, *, wait=0):
        self.waits.append(wait)
        status, took = next(self.answers)
        self.clock[0] += wait if took is None else took
        return {"id": task_id, "status": status, "deadline_at": "2099-01-01T00:00:00Z"}, "req"


class WaitLoopTests(unittest.TestCase):
    def setUp(self):
        self.clock = [1000.0]
        monotonic = patch.object(tasks.time, "monotonic", side_effect=lambda: self.clock[0])
        monotonic.start()
        self.addCleanup(monotonic.stop)
        self.sleeps = []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.clock[0] += seconds

    def wait(self, client, **options):
        queued = {"id": "task_wait", "status": "queued", "deadline_at": "2099-01-01T00:00:00Z"}
        return tasks.wait_for_terminal(client, queued, None, sleep=self.sleep, **options)[0]

    def test_early_unchanged_answers_are_spaced_out(self):
        # A service answering at once without a change must not be asked again in a tight loop.
        client = ScriptedClient(self.clock, [
            ("running", 0.0), ("running", 0.0), ("running", 0.5), ("running", None), ("succeeded", 0.0),
        ])
        result = self.wait(client)
        self.assertEqual(result["status"], "succeeded")
        # The change to running and the answer that used its whole wait need no pause.
        self.assertEqual(self.sleeps, [2.0, 1.5])
        self.assertEqual(client.waits, [TASK_WAIT_SECONDS] * 5)

    def test_max_wait_returns_the_running_task_without_asking_past_it(self):
        # The service must not be asked to hold a request beyond the caller's limit.
        client = ScriptedClient(self.clock, [("running", None)] * 3)
        result = self.wait(client, max_wait=70.5)
        self.assertEqual(result["status"], "running")
        self.assertEqual(client.waits, [30, 30, 10])
        self.assertEqual(self.clock[0], 1070.0)
        self.assertEqual(self.sleeps, [])


class SubmissionWaitTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("ASYNX_")}
        environment.update({
            "ASYNX_CONFIG_PATH": str(self.root / "config.json"),
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CACHE_PATH": str(self.root / "cache.json"),
        })
        env = patch.dict(os.environ, environment, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = FakeClient()

    def args(self, **overrides):
        values = {
            "prompt": "cat", "model": None, "image_size": None, "aspect_ratio": None, "quality": None,
            "count": 1, "output_format": None, "reference": [], "image": [], "mask": None,
            "keep_reference_original": False, "idempotency_key": "asx-waiting-test",
            "output_dir": str(self.root / "outputs"), "detach": False, "from_task": None,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_new_submission_waits_on_the_service_for_its_result(self):
        with patch.object(self.client, "task", wraps=self.client.task) as query:
            result, code = tasks.run_submission(self.client, self.args(), "image.generate")
        self.assertEqual((code, result["ok"]), (0, True))
        query.assert_called_once_with(result["task_id"], wait=TASK_WAIT_SECONDS)
        self.assertEqual(len(self.client.submitted), 1)

    def test_reused_submission_waits_for_the_original_task_without_submitting_again(self):
        submitted, _code = tasks.run_submission(self.client, self.args(detach=True), "image.generate")
        final, request_id = self.client.task(submitted["task_id"])
        responses = [({**final, "status": "running"}, request_id), (final, request_id)]
        with patch.object(self.client, "task", side_effect=responses):
            result, code = tasks.run_submission(self.client, self.args(), "image.generate")
        self.assertEqual((code, result["ok"]), (0, True))
        self.assertEqual(len(self.client.submitted), 1)


if __name__ == "__main__":
    unittest.main()
