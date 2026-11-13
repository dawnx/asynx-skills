from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import cli, tasks
from asxlib.errors import AsxError
from test_audit_regressions import FakeClient
from test_video_downloads import MP4


class VideoClient(FakeClient):
    fail_second = False
    poster_only = False

    def task(self, task_id, *, wait=0):
        task, request_id = super().task(task_id)
        task["task_type"] = "video.generate"
        task["result"]["assets"] = [{"index": 2}, {"index": 5}]
        return task, request_id

    def asset_to_file(self, task_id, index, destination, url, *, metrics):
        if index == 5 and self.fail_second:
            raise AsxError("下载中断", code="asset_download_failed")
        self.downloaded.append(index)
        destination.write_bytes(self.png if index == 2 or self.poster_only else MP4)
        return "image/png" if index == 2 or self.poster_only else "video/mp4"


class VideoTaskTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CONFIG_PATH": str(self.root / "config.json"),
            "ASYNX_CACHE_PATH": str(self.root / "cache.json"),
            "ASYNX_UPDATE_CHECK": "off",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = VideoClient()

    def submit(self):
        args = cli.parser().parse_args([
            "video", "generate", "--prompt", "鸟鸣声中的树林", "--detach",
            "--output-dir", str(self.root / "outputs"),
        ])
        return tasks.run_submission(self.client, args, "video.generate")[0]

    def test_partial_mixed_download_recovers_original_indices_without_resubmission(self):
        submitted = self.submit()
        self.client.fail_second = True
        result, code = tasks.refresh_task(self.client, submitted["local_id"])
        self.assertEqual(code, 4)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(result["videos"], [])
        self.assertEqual(result["error"]["phase"], "download")
        self.client.fail_second = False
        result, code = tasks.wait_and_download(self.client, submitted["task_id"])
        self.assertEqual(code, 0)
        self.assertEqual(len(result["images"]), 1)
        self.assertEqual(len(result["videos"]), 1)
        self.assertEqual(self.client.downloaded, [2, 5])
        self.assertEqual(len(self.client.submitted), 1)
        assets = tasks.local_asset_payload(submitted["task_id"])["assets"]
        self.assertEqual([item["remote_index"] for item in assets], [2, 5])

    def test_poster_only_is_not_delivered_as_a_successful_video(self):
        submitted = self.submit()
        self.client.poster_only = True
        result, code = tasks.refresh_task(self.client, submitted["task_id"])
        self.assertEqual(code, 4)
        self.assertEqual(result["error"]["code"], "missing_video_asset")
        self.assertEqual(len(result["images"]), 2)
        self.assertEqual(result["videos"], [])

    def test_scoped_recovery_reuses_remote_video_and_cancel_does_not_resubmit(self):
        submitted = self.submit()
        recovered = tasks.recover_tasks(self.client, submitted["local_id"])
        self.assertEqual(len(recovered["tasks"][0]["videos"]), 1)
        tasks.cancel_local_task(self.client, submitted["local_id"])
        tasks.recover_tasks(self.client, submitted["local_id"])
        self.assertEqual(len(self.client.submitted), 1)

    def test_video_wait_retains_type(self):
        queued = {"id": "task_wait", "status": "queued", "task_type": "video.generate"}
        with patch.object(self.client, "task", side_effect=[
            ({"id": "task_wait", "status": "running"}, None),
            ({"id": "task_wait", "status": "succeeded"}, None),
        ]):
            result, _ = tasks.wait_for_terminal(self.client, queued, None)
        self.assertEqual(result["task_type"], "video.generate")


if __name__ == "__main__":
    unittest.main()
