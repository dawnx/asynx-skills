from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))
from asxlib import batches, tasks
from asxlib.errors import AsxError
from test_audit_regressions import FakeClient


class VideoBatchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        env = patch.dict(os.environ, {"ASYNX_STATE_PATH": str(self.root / "state.db"),
                                     "ASYNX_CACHE_PATH": str(self.root / "cache.json"),
                                     "ASYNX_UPDATE_CHECK": "off"})
        env.start()
        self.addCleanup(env.stop)
        self.client = FakeClient()

    def create(self, **overrides):
        kwargs = {"operation": "video.generate", "prompt": "产品转动", "model_selector": None,
                  "image_size": None, "aspect_ratio": None, "quality": None, "count": 1, "output_format": None,
                  "references": [], "mask": None, "total": 2, "name": None, "output_dir": str(self.root / "outputs")}
        kwargs.update(overrides)
        return batches.create_batch(self.client, **kwargs)["id"]

    def add(self, batch_id, **overrides):
        kwargs = {"total": 1, "prompt": None, "references": None, "mask": None, "count": None,
                  "image_size": None, "aspect_ratio": None, "quality": None, "output_format": None}
        kwargs.update(overrides)
        return batches.add_to_batch(self.client, batch_id, **kwargs)

    def bodies(self, batch_id):
        with batches.connect_db() as connection:
            batch = batches.get_batch(connection, batch_id)
            template = json.loads(batch["template_json"])
            rows = connection.execute("SELECT body_json FROM batch_items WHERE batch_id=? ORDER BY sequence", (batch_id,)).fetchall()
            return [template if (body := json.loads(row["body_json"])).get("_asx_inherit_template") else body for row in rows]

    def test_video_template_has_distinct_public_task_type_without_schema_rebuild(self):
        batch_id = self.create()
        summary = batches.batch_status(batch_id)["batch"]
        self.assertEqual(summary["task_type"], "video.generate")
        self.assertEqual(summary["modality"], "video")
        self.assertEqual(summary["operation"], "generate")
        self.assertEqual(self.bodies(batch_id)[0]["input"]["duration"], 5)
        self.assertEqual(self.client.submitted, [])

    def test_add_inherits_and_overrides_video_fields_without_changing_template(self):
        batch_id = self.create(first_frame="https://example.test/start.png")
        self.add(batch_id)
        self.add(batch_id, prompt="new", duration=7.25, resolution="2K", aspect_ratio="9:16",
                 first_frame="https://example.test/new.png", last_frame="https://example.test/end.png",
                 reference_videos=["https://example.test/video.mp4"], reference_audios=["https://example.test/audio.mp3"])
        values = self.bodies(batch_id)
        self.assertEqual(values[2], values[0])
        self.assertEqual(values[3]["input"]["duration"], 7.25)
        self.assertEqual(values[0]["input"]["duration"], 5)
        self.assertEqual([i["type"] for i in values[3]["input"]["inputs"]], ["image", "image", "video", "audio"])

    def test_pause_refresh_and_cancellation_never_submit_video(self):
        batch_id = self.create()
        batches.set_batch_paused(batch_id, True)
        batches.refresh_batch(self.client, batch_id)
        batches.wait_for_batch(self.client, batch_id, interval=1, max_wait=1)
        self.assertEqual(self.client.submitted, [])
        batches.cancel_batch(self.client, batch_id)
        tasks.recover_tasks(self.client)
        self.assertEqual(self.client.submitted, [])

    def test_video_poll_creates_correct_intents_and_keeps_pure_query_pure(self):
        batch_id = self.create()
        self.client.statuses["task_1"] = "running"
        result = batches.poll_batch(self.client, batch_id, submissions_limit=1)
        self.assertEqual(result["batch"]["counts"], {"pending": 1, "running": 1})
        self.assertEqual(self.client.submitted[0][0]["task_type"], "video.generate")
        self.assertEqual(tasks.local_tasks_payload()["tasks"][0]["operation"], "video.generate")
        batches.refresh_batch(self.client, batch_id)
        self.assertEqual(len(self.client.submitted), 1)

    def test_image_only_options_cannot_be_silently_ignored(self):
        with self.assertRaises(AsxError):
            self.create(quality="high")
        batch_id = self.create()
        with self.assertRaises(AsxError):
            self.add(batch_id, output_format="png")

    def test_scalar_overrides_reuse_prepared_images_without_reencoding(self):
        from PIL import Image
        source = self.root / "source.png"
        Image.new("RGB", (50, 50), "red").save(source)
        batch_id = self.create(references=[str(source)], first_frame=str(source))
        original_inputs = self.bodies(batch_id)[0]["input"]["inputs"]
        with patch.object(batches, "prepare_video_image", side_effect=AssertionError("unexpected reencoding")):
            self.add(batch_id, prompt="new prompt", duration=6.5)
        self.assertEqual(self.bodies(batch_id)[2]["input"]["inputs"], original_inputs)


if __name__ == "__main__":
    unittest.main()
