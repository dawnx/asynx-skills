from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.request import urlopen

import test_cli_e2e as image_e2e


class VideoCLITests(unittest.TestCase):
    setUp = image_e2e.CLIE2ETestCase.setUp
    tearDown = image_e2e.CLIE2ETestCase.tearDown
    run_cli = image_e2e.CLIE2ETestCase.run_cli

    def stats(self):
        with urlopen(self.base_url + "/__mock__/stats", timeout=5) as response:
            return json.load(response)["data"]

    def complete(self, identifier):
        self.run_cli("task", "status", identifier, "--refresh")
        return self.run_cli("task", "status", identifier, "--refresh")["task"]

    def test_detached_video_refresh_download_and_missing_file_recovery(self):
        submitted = self.run_cli(
            "video", "generate", "--prompt", "海浪拍岸，有清晰海浪声", "--detach",
            "--output-dir", str(self.temp_path / "video"),
        )
        remote = self.run_cli("history", "--limit", "1")["tasks"][0]
        self.assertEqual(remote["task_type"], "video.generate")
        self.assertEqual(remote["model"], "MiniMax-H3")
        self.assertEqual(remote["input"]["duration"], 5)
        self.assertEqual(remote["input"]["resolution"], "720p")
        self.assertNotIn("generate_audio", remote["input"])
        completed = self.complete(submitted["local_id"])
        self.assertEqual(completed["images"], [])
        self.assertEqual(len(completed["videos"]), 1)
        self.assertEqual(completed["media"][0]["kind"], "video")
        path = Path(completed["files"][0])
        self.assertEqual(path.suffix, ".mp4")
        original = path.read_bytes()
        path.unlink()
        waited = self.run_cli("wait", submitted["task_id"])
        self.assertEqual(waited["files"], [str(path)])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.stats()["submissions"], 1)
        recent = self.run_cli("recent", "--modality", "video", "--latest", with_credentials=False)
        self.assertEqual(recent["artifacts"][0]["task_type"], "video.generate")
        self.assertEqual(recent["artifacts"][0]["kind"], "video")
        self.assertEqual(self.run_cli("recent", "--modality", "image", with_credentials=False)["artifacts"], [])

    def test_single_command_waits_and_downloads_video(self):
        completed = self.run_cli(
            "video", "generate", "--prompt", "慢慢升起的气球", "--duration", "7.5",
            "--resolution", "1080p", "--output-dir", str(self.temp_path / "video"),
        )
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["task_type"], "video.generate")
        self.assertEqual(len(completed["videos"]), 1)
        self.assertNotIn("has_audio", completed["videos"][0])
        remote = self.run_cli("history", "--limit", "1")["tasks"][0]
        self.assertEqual(remote["input"]["duration"], 7.5)
        self.assertEqual(self.stats()["model_requests"], 0)

    def test_image_task_is_used_as_video_first_frame_and_video_cannot_be_used_as_image(self):
        source = self.run_cli(
            "generate", "--prompt", "产品图片", "--detach",
            "--output-dir", str(self.temp_path / "image"),
        )
        self.complete(source["local_id"])
        video = self.run_cli(
            "video", "generate", "--from-task", source["task_id"], "--prompt", "产品旋转",
            "--detach", "--output-dir", str(self.temp_path / "video"),
        )
        remote = self.run_cli("history", "--limit", "1")["tasks"][0]
        self.assertEqual(remote["input"]["inputs"][0]["role"], "first_frame")
        self.assertTrue(remote["input"]["inputs"][0]["url"].startswith(("data:image/jpeg;base64,", "data:image/png;base64,")))
        self.complete(video["task_id"])
        failed = self.run_cli(
            "video", "generate", "--from-task", video["task_id"], "--prompt", "wrong source",
            "--detach", expected_returncode=2,
        )
        self.assertEqual(failed["error"]["code"], "ambiguous_video_source")
        self.assertEqual(self.stats()["submissions"], 2)

    def test_video_batch_pause_refresh_append_resume_wait(self):
        created = self.run_cli(
            "batch", "create", "--operation", "video.generate", "--prompt", "蓝色纸盒旋转",
            "--total", "2", "--limit", "1", "--output-dir", str(self.temp_path / "batch"),
        )
        batch_id = created["batch"]["id"]
        self.run_cli("batch", "pause", batch_id)
        paused = self.run_cli("batch", "status", batch_id, "--refresh")["batch"]
        self.assertEqual(paused["counts"], {"pending": 1, "succeeded": 1})
        self.assertEqual(len(paused["videos"]), 1)
        self.assertEqual(self.stats()["submissions"], 1)
        self.run_cli("batch", "add", batch_id, "--total", "1", "--duration", "9.5")
        self.assertEqual(self.stats()["submissions"], 1)
        self.run_cli("batch", "resume", batch_id)
        finished = self.run_cli("batch", "wait", batch_id, "--interval", "1")["batch"]
        self.assertEqual(finished["task_type"], "video.generate")
        self.assertEqual(finished["counts"], {"succeeded": 3})
        self.assertEqual(len(finished["videos"]), 3)
        self.assertEqual(finished["images"], [])
        self.assertEqual(self.stats()["submissions"], 3)

    def test_invalid_video_requests_are_rejected_before_submission(self):
        for flags in (
            ["--model", "seedance-2-0"], ["--resolution", "768p"],
            ["--last-frame", "https://example.test/end.png"],
            ["--reference-video", "/tmp/private.mp4"],
        ):
            with self.subTest(flags=flags):
                result = self.run_cli("video", "generate", "--prompt", "test", *flags, expected_returncode=2)
                self.assertFalse(result["ok"])
        self.assertEqual(self.stats()["submissions"], 0)


if __name__ == "__main__":
    unittest.main()
