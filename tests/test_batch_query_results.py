from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import batches
from asxlib.errors import AsxError
from test_audit_regressions import FakeClient


class BatchQueryResultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = {key: value for key, value in os.environ.items() if not key.startswith("ASYNX_")}
        environment.update({
            "ASYNX_CONFIG_PATH": str(self.root / "config.json"),
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CACHE_PATH": str(self.root / "cache.json"),
            "ASYNX_UPDATE_CHECK": "off",
        })
        env = patch.dict(os.environ, environment, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = FakeClient()

    def create(self, total=1):
        return batches.create_batch(
            self.client, operation="generate", prompt="cat", model_selector=None,
            image_size=None, aspect_ratio=None, quality=None, count=1,
            output_format=None, references=[], mask=None, total=total, name=None,
            output_dir=str(self.root / "outputs"),
        )["id"]

    def test_refresh_never_submits_pending_or_recovers_stale_submitting(self):
        batch_id = self.create(total=2)
        connection = batches.connect_db()
        try:
            connection.execute(
                "UPDATE batch_items SET status = 'submitting', updated_at = '2000-01-01' "
                "WHERE batch_id = ? AND sequence = 1", (batch_id,),
            )
            connection.commit()
        finally:
            connection.close()
        result = batches.refresh_batch(self.client, batch_id)
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(result["batch"]["counts"], {"pending": 1, "submitting": 1})
        self.assertEqual(result["batch"]["submitted_now"], 0)

    def test_status_refresh_is_pure_and_returns_outputs_without_items_option(self):
        batch_id = self.create(total=2)
        self.client.statuses["task_1"] = "running"
        batches.poll_batch(self.client, batch_id, submissions_limit=1)
        self.client.statuses["task_1"] = "succeeded"
        result, code = batches.execute_batch(
            self.client, argparse.Namespace(
                batch_command="status", batch_id=batch_id, refresh=True, items=False,
            ),
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(self.client.submitted), 1)
        self.assertEqual(result["batch"]["counts"], {"pending": 1, "succeeded": 1})
        self.assertEqual(len(result["batch"]["files"]), 1)
        self.assertEqual(result["batch"]["images"][0]["width"], 4)
        self.assertEqual(result["batch"]["images"][0]["sequence"], 1)

    def test_refresh_downloads_running_task_while_batch_is_paused(self):
        batch_id = self.create(total=2)
        self.client.statuses["task_1"] = "running"
        batches.poll_batch(self.client, batch_id, submissions_limit=1)
        batches.set_batch_paused(batch_id, True)
        self.client.statuses["task_1"] = "succeeded"
        result = batches.refresh_batch(self.client, batch_id)
        self.assertEqual(len(self.client.submitted), 1)
        self.assertEqual(result["batch"]["status"], "paused")
        self.assertEqual(result["batch"]["counts"], {"pending": 1, "succeeded": 1})
        self.assertEqual(len(result["batch"]["files"]), 1)

    def test_results_include_failures_download_errors_and_degraded_warning(self):
        batch_id = self.create(total=3)
        original_task = self.client.task
        original_asset = self.client.asset

        def task(task_id):
            result, request_id = original_task(task_id)
            if task_id == "task_1":
                result["result_quality"] = "degraded"
            if task_id == "task_2":
                result.update(status="failed", error={"code": "rejected", "message": "拒绝"})
            return result, request_id

        def asset(task_id, *args, **kwargs):
            self.client.reject_download = task_id == "task_3"
            return original_asset(task_id, *args, **kwargs)

        with patch.object(self.client, "task", side_effect=task), patch.object(
            self.client, "asset", side_effect=asset,
        ):
            result = batches.poll_batch(self.client, batch_id)
        summary = result["batch"]
        self.assertFalse(result["ok"])
        self.assertTrue(summary["partial"])
        self.assertEqual(len(summary["files"]), 1)
        self.assertEqual(summary["failed_items"][0]["sequence"], 2)
        self.assertEqual(summary["failed_items"][0]["error"]["code"], "rejected")
        self.assertEqual(summary["download_errors"][0]["sequence"], 3)
        self.assertEqual(summary["download_errors"][0]["error"]["code"], "network_error")
        self.assertIn("result_degraded", [warning["code"] for warning in summary["warnings"]])
        # A later local-only read retains these delivery and failure details.
        snapshot = batches.batch_status(batch_id)["batch"]
        self.assertEqual(snapshot["failed_items"], summary["failed_items"])
        self.assertEqual(snapshot["download_errors"], summary["download_errors"])
        self.assertIn("result_degraded", [warning["code"] for warning in snapshot["warnings"]])

    def test_wait_reads_image_metadata_only_when_returning_results(self):
        batch_id = self.create(total=2)
        original_task = self.client.task
        calls = {}

        def task(task_id):
            calls[task_id] = calls.get(task_id, 0) + 1
            self.client.statuses[task_id] = (
                "running" if task_id == "task_2" and calls[task_id] < 3 else "succeeded"
            )
            return original_task(task_id)

        with patch.object(self.client, "task", side_effect=task), patch.object(
            batches.time, "sleep",
        ), patch.object(
            batches, "attach_image_details", wraps=batches.attach_image_details,
        ) as metadata:
            result = batches.wait_for_batch(self.client, batch_id, interval=1, max_wait=10)
        self.assertEqual(result["batch"]["status"], "completed")
        self.assertEqual(len(result["batch"]["files"]), 2)
        self.assertEqual(metadata.call_count, 2)
        self.assertEqual(len(self.client.submitted), 2)

    def test_completed_poll_includes_files_and_missing_file_can_be_downloaded_again(self):
        batch_id = self.create()
        first = batches.poll_batch(self.client, batch_id)
        completed = batches.poll_batch(self.client, batch_id)
        self.assertEqual(completed["batch"]["files"], first["batch"]["files"])
        Path(first["batch"]["files"][0]).unlink()
        snapshot = batches.batch_status(batch_id)["batch"]
        self.assertEqual(snapshot["files"], [])
        self.assertEqual(snapshot["download_errors"][0]["error"]["code"], "download_pending")
        refreshed = batches.refresh_batch(self.client, batch_id)
        self.assertEqual(len(refreshed["batch"]["files"]), 1)
        self.assertEqual(refreshed["batch"]["download_errors"], [])
        self.assertEqual(len(self.client.submitted), 1)

    def test_poll_still_advances_queue(self):
        batch_id = self.create(total=3)
        first = batches.poll_batch(self.client, batch_id, submissions_limit=1)
        self.assertEqual(first["batch"]["submitted_now"], 1)
        second = batches.poll_batch(self.client, batch_id, submissions_limit=2)
        self.assertEqual(second["batch"]["submitted_now"], 2)
        self.assertEqual(second["batch"]["status"], "completed")
        self.assertEqual(len(second["batch"]["files"]), 3)

    def test_partial_download_is_deliverable_but_not_marked_complete(self):
        batch_id = self.create()
        original_task = self.client.task
        original_asset = self.client.asset
        asset_requests = []
        fail_second = True

        def task(task_id):
            result, request_id = original_task(task_id)
            result["result"] = {"assets": [{"index": 0}, {"index": 1}]}
            return result, request_id

        def asset(task_id, index, *args, **kwargs):
            asset_requests.append(index)
            if index == 1 and fail_second:
                raise AsxError("download interrupted", code="network_error")
            return original_asset(task_id, index, *args, **kwargs)

        with patch.object(self.client, "task", side_effect=task), patch.object(
            self.client, "asset", side_effect=asset,
        ):
            first = batches.poll_batch(self.client, batch_id)["batch"]
            self.assertEqual(first["pending_download"], 1)
            self.assertEqual(first["finished"], 0)
            self.assertEqual(first["status"], "active")
            self.assertEqual(len(first["files"]), 1)
            self.assertEqual(len(first["images"]), 1)
            self.assertEqual(first["download_errors"][0]["error"]["code"], "network_error")
            self.assertEqual(first["next_action_args"], ["batch", "wait", batch_id])
            snapshot = batches.batch_status(batch_id)["batch"]
            self.assertEqual(snapshot["files"], first["files"])
            self.assertEqual(snapshot["pending_download"], 1)
            # Older interrupted batches may only have the saved asset in the ledger.
            connection = batches.connect_db()
            try:
                connection.execute(
                    "UPDATE batch_items SET files_json = NULL WHERE batch_id = ?", (batch_id,),
                )
                connection.commit()
            finally:
                connection.close()
            old_snapshot = batches.batch_status(batch_id)["batch"]
            self.assertEqual(old_snapshot["files"], first["files"])
            self.assertEqual(old_snapshot["pending_download"], 1)
            fail_second = False
            recovered = batches.refresh_batch(self.client, batch_id)["batch"]
        self.assertEqual(recovered["status"], "completed")
        self.assertEqual(recovered["pending_download"], 0)
        self.assertEqual(len(recovered["files"]), 2)
        self.assertEqual(recovered["download_errors"], [])
        self.assertNotIn("next_action_args", recovered)
        self.assertEqual(asset_requests, [0, 1, 1])
        self.assertEqual(len(self.client.submitted), 1)

    def test_wait_on_paused_batch_returns_without_sleep_or_submission(self):
        batch_id = self.create()
        batches.set_batch_paused(batch_id, True)
        with patch.object(batches.time, "sleep") as sleep:
            result = batches.wait_for_batch(self.client, batch_id, interval=1, max_wait=0)
        sleep.assert_not_called()
        self.assertTrue(result["paused"])
        self.assertEqual(result["batch"]["next_action_args"], ["batch", "status", batch_id, "--refresh"])
        self.assertIn("暂停", result["batch"]["action_required"])
        self.assertEqual(self.client.submitted, [])

    def test_refresh_can_complete_paused_batch_without_resuming(self):
        batch_id = self.create()
        self.client.statuses["task_1"] = "running"
        batches.poll_batch(self.client, batch_id)
        batches.set_batch_paused(batch_id, True)
        self.client.statuses["task_1"] = "succeeded"
        result = batches.refresh_batch(self.client, batch_id)
        self.assertEqual(result["batch"]["status"], "completed")
        self.assertEqual(len(result["batch"]["files"]), 1)
        self.assertNotIn("next_action_args", result["batch"])
        self.assertEqual(len(self.client.submitted), 1)

    def test_refresh_query_errors_are_reported_and_do_not_submit_pending_items(self):
        for http_status in (None, 404):
            with self.subTest(http_status=http_status):
                self.client = FakeClient()
                batch_id = self.create(total=2)
                self.client.statuses["task_1"] = "running"
                batches.poll_batch(self.client, batch_id, submissions_limit=1)
                error = AsxError("query unavailable", code="query_failed", http_status=http_status)
                with patch.object(self.client, "task", side_effect=error):
                    result, code = batches.execute_batch(
                        self.client, argparse.Namespace(
                            batch_command="status", batch_id=batch_id, refresh=True, items=False,
                        ),
                    )
                self.assertEqual(code, 4)
                self.assertFalse(result["ok"])
                self.assertEqual(result["batch"]["refresh_errors"][0]["error"]["code"], "query_failed")
                self.assertIn("batch_refresh_incomplete", [item["code"] for item in result["batch"]["warnings"]])
                self.assertEqual(result["batch"]["next_action_args"], ["batch", "status", batch_id, "--refresh"])
                self.assertEqual(len(self.client.submitted), 1)
                # Prevent fake remote task IDs colliding between subtests.
                connection = batches.connect_db()
                try:
                    connection.execute("DELETE FROM batch_items WHERE batch_id = ?", (batch_id,))
                    connection.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
                    connection.execute("DELETE FROM tasks")
                    connection.commit()
                finally:
                    connection.close()

    def test_pending_snapshot_gives_scoped_wait_action(self):
        batch_id = self.create()
        result = batches.batch_status(batch_id)["batch"]
        self.assertEqual(result["files"], [])
        self.assertEqual(result["next_action_args"], ["batch", "wait", batch_id])

    def test_unknown_remote_status_is_reported_as_refresh_failure(self):
        batch_id = self.create()
        self.client.statuses["task_1"] = "running"
        batches.poll_batch(self.client, batch_id)
        self.client.statuses["task_1"] = "unexpected-status"
        result, code = batches.execute_batch(
            self.client, argparse.Namespace(
                batch_command="status", batch_id=batch_id, refresh=True, items=False,
            ),
        )
        self.assertEqual(code, 4)
        self.assertFalse(result["ok"])
        self.assertEqual(result["batch"]["refresh_errors"][0]["error"]["code"], "invalid_task_status")
        self.assertEqual(result["batch"]["counts"], {"running": 1})


if __name__ == "__main__":
    unittest.main()
