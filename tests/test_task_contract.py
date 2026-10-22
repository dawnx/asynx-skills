from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import batches, cli, state, tasks
from asxlib.constants import DEFAULT_MODEL
from asxlib.errors import AsxError
from test_audit_regressions import FakeClient


class ResultClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.queried = []
        self.asset_attempts = []
        self.asset_count = 1
        self.failing_index = None

    def task(self, task_id):
        self.queried.append(task_id)
        result, request_id = super().task(task_id)
        result["result"]["assets"] = [
            {"index": index} for index in range(self.asset_count)
        ]
        return result, request_id

    def asset(self, task_id, index, url, *, metrics):
        self.asset_attempts.append((task_id, index))
        if index == self.failing_index:
            raise AsxError("download interrupted", code="network_error")
        return super().asset(task_id, index, url, metrics=metrics)


class TaskContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith("ASYNX_")
        }
        environment.update({
            "ASYNX_CONFIG_PATH": str(self.root / "config.json"),
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CACHE_PATH": str(self.root / "cache.json"),
            "ASYNX_UPDATE_CHECK": "off",
        })
        env = patch.dict(os.environ, environment, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = ResultClient()

    def intent(self, name, *, remote_id=None, status="intent"):
        with state.connect_db() as connection:
            row = state.create_task_intent(
                connection,
                operation="generate",
                model=DEFAULT_MODEL,
                idempotency_key=f"asx-contract-{name}",
                request={
                    "task_type": "image.generate", "model": DEFAULT_MODEL,
                    "input": {"prompt": name, "aspect_ratio": "1:1"},
                },
                output_dir=str(self.root / name),
                base_url=self.client.base_url,
                local_id=f"local_{name}",
                status=status,
            )
            if remote_id:
                row = state.bind_remote_task(
                    connection, row["local_id"], remote_id,
                    status=status,
                    remote={"id": remote_id, "status": status},
                )
            return row

    def execute(self, arguments):
        with patch.object(cli, "_client", return_value=self.client):
            return cli.execute(cli.parser().parse_args(arguments))

    def test_refresh_by_local_or_remote_id_downloads_without_submission(self):
        row = self.intent("bound", remote_id="task_bound", status="queued")
        for identifier in (row["local_id"], "task_bound"):
            with self.subTest(identifier=identifier):
                result, code = self.execute(["task", "status", identifier, "--refresh"])
                self.assertEqual(code, 0)
                self.assertTrue(result["ok"])
                task = result["task"]
                self.assertEqual(task["local_id"], row["local_id"])
                self.assertEqual(task["task_id"], "task_bound")
                self.assertEqual(task["status"], "succeeded")
                self.assertEqual(len(task["files"]), 1)
                self.assertEqual(task["images"][0]["width"], 4)
                self.assertTrue(Path(task["files"][0]).is_file())
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, ["task_bound", "task_bound"])
        self.assertEqual(self.client.asset_attempts, [("task_bound", 0)])

    def test_local_snapshot_needs_no_client_and_does_not_refresh(self):
        row = self.intent("snapshot", remote_id="task_snapshot", status="queued")
        with patch.object(cli, "_client", side_effect=AssertionError("must remain offline")):
            result, code = cli.execute(cli.parser().parse_args([
                "task", "status", row["local_id"],
            ]))
        self.assertEqual(code, 0)
        self.assertEqual(result["task"]["status"], "queued")

    def test_refresh_unsubmitted_intent_only_offers_scoped_recovery(self):
        row = self.intent("unsubmitted")
        result, code = self.execute(["task", "status", row["local_id"], "--refresh"])
        self.assertEqual(code, 0)
        self.assertEqual(result["task"]["status"], "intent")
        self.assertEqual(
            result["task"]["next_action_args"], ["task", "recover", row["local_id"]],
        )
        self.assertEqual(self.client.queried, [])
        self.assertEqual(self.client.submitted, [])

    def test_unknown_local_refresh_does_not_query_remote_or_submit(self):
        self.intent("unrelated")
        with self.assertRaises(AsxError) as caught:
            self.execute(["task", "status", "local_missing", "--refresh"])
        self.assertEqual(caught.exception.code, "task_not_found")
        self.assertEqual(self.client.queried, [])
        self.assertEqual(self.client.submitted, [])

    def test_untracked_remote_refresh_downloads_without_submission(self):
        with patch.object(tasks, "DEFAULT_OUTPUT_DIR", str(self.root / "untracked")):
            result, code = self.execute(["task", "status", "task_untracked", "--refresh"])
        self.assertEqual(code, 0)
        self.assertEqual(result["task"]["task_id"], "task_untracked")
        self.assertEqual(len(result["task"]["files"]), 1)
        self.assertTrue(Path(result["task"]["files"][0]).is_file())
        self.assertEqual(self.client.submitted, [])

    def test_refresh_rejects_unknown_remote_status(self):
        self.client.statuses["task_unknown_status"] = "unexpected_state"
        with self.assertRaises(AsxError) as caught:
            self.execute(["task", "status", "task_unknown_status", "--refresh"])
        self.assertEqual(caught.exception.code, "invalid_task_status")
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.asset_attempts, [])

    def test_scoped_recovery_reuses_key_and_leaves_other_intents_untouched(self):
        chosen = self.intent("chosen")
        other = self.intent("other")
        result, code = self.execute(["task", "recover", chosen["local_id"], "--limit", "1"])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["local_id"], chosen["local_id"])
        self.assertEqual(self.client.submitted, [(chosen["request"], chosen["idempotency_key"])])
        unchanged = tasks.local_task_status(other["local_id"])["task"]
        self.assertEqual(unchanged["status"], "intent")
        self.assertNotIn("task_id", unchanged)

    def test_scoped_recovery_accepts_remote_id_without_resubmitting(self):
        row = self.intent("existing", remote_id="task_existing", status="queued")
        self.intent("pending")
        result = tasks.recover_tasks(self.client, "task_existing")
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["local_id"], row["local_id"])
        self.assertEqual(self.client.submitted, [])

    def test_scoped_recovery_preserves_paused_batch_without_submitting(self):
        batch_id = batches.create_batch(
            self.client, operation="generate", prompt="cat", model_selector=None,
            image_size=None, aspect_ratio=None, quality=None, count=1,
            output_format=None, references=[], mask=None, total=1, name=None,
            output_dir=str(self.root / "paused_batch"),
        )["id"]
        self.client.reject_submit = True
        batches.poll_batch(self.client, batch_id)
        local_id = tasks.local_tasks_payload()["tasks"][0]["local_id"]
        batches.set_batch_paused(batch_id, True)
        self.client.reject_submit = False
        result, code = self.execute(["task", "recover", local_id])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["tasks"]), 1)
        task = result["tasks"][0]
        self.assertEqual(task["local_id"], local_id)
        self.assertTrue(task["paused"])
        self.assertIn("batch_paused", [warning["code"] for warning in task["warnings"]])
        self.assertEqual(task["next_action_args"], ["batch", "status", batch_id, "--refresh"])
        self.assertEqual(batches.batch_status(batch_id)["batch"]["status"], "paused")
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, [])
        self.assertNotIn("task_id", tasks.local_task_status(local_id)["task"])

    def test_missing_recovery_id_never_falls_back_to_all_tasks(self):
        self.intent("pending")
        for identifier in ("local_missing", "task_missing"):
            with self.subTest(identifier=identifier):
                with self.assertRaises(AsxError) as caught:
                    self.execute(["task", "recover", identifier])
                self.assertEqual(caught.exception.code, "task_not_found")
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, [])

    def test_recovery_does_not_resubmit_failed_or_canceled_records(self):
        for status in ("failed", "timeout", "canceled"):
            for bound in (False, True):
                with self.subTest(status=status, bound=bound):
                    name = f"{status}_{bound}"
                    row = self.intent(
                        name, status=status, remote_id=f"task_{name}" if bound else None,
                    )
                    result, code = self.execute(["task", "recover", row["local_id"]])
                    self.assertEqual(code, 4)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["tasks"][0]["status"], status)
                    self.assertNotIn("next_action_args", result["tasks"][0])
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, [])

    def test_partial_download_recovers_only_missing_asset_without_regeneration(self):
        row = self.intent("partial", remote_id="task_partial", status="queued")
        self.client.asset_count = 2
        self.client.failing_index = 1
        result, code = self.execute(["task", "status", row["local_id"], "--refresh"])
        self.assertEqual(code, 4)
        self.assertFalse(result["ok"])
        task = result["task"]
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(task["download_status"], "failed")
        self.assertEqual(task["error"]["phase"], "download")
        self.assertEqual(task["next_action_args"], ["wait", row["local_id"]])
        self.assertEqual(len(task["files"]), 1)
        self.assertEqual(len(task["images"]), 1)
        first_path = Path(task["files"][0])
        first_contents = first_path.read_bytes()
        self.client.failing_index = None
        completed, code = self.execute(task["next_action_args"])
        self.assertEqual(code, 0)
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["download_status"], "downloaded")
        self.assertEqual(len(completed["files"]), 2)
        self.assertEqual(len(completed["images"]), 2)
        self.assertEqual(first_path.read_bytes(), first_contents)
        self.assertTrue(all(Path(path).is_file() for path in completed["files"]))
        self.assertNotIn("next_action_args", completed)
        self.assertEqual(self.client.asset_attempts, [
            ("task_partial", 0), ("task_partial", 1), ("task_partial", 1),
        ])
        self.assertEqual(self.client.submitted, [])

    def test_wait_on_unsubmitted_intent_offers_recovery_without_submitting(self):
        row = self.intent("awaiting_submit")
        with self.assertRaises(AsxError) as caught:
            self.execute(["wait", row["local_id"]])
        self.assertEqual(caught.exception.code, "task_not_submitted")
        self.assertEqual(
            caught.exception.details["next_action_args"], ["task", "recover", row["local_id"]],
        )
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(self.client.queried, [])

    def test_legacy_recovery_without_identifier_retains_explicit_limit(self):
        parsed = cli.parser().parse_args(["task", "recover", "--limit", "3"])
        self.assertIsNone(parsed.task_id)
        self.assertEqual(parsed.limit, 3)


if __name__ == "__main__":
    unittest.main()
