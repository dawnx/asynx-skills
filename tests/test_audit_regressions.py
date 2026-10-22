from __future__ import annotations

import argparse
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import batches, images, local_images, state, tasks
from asxlib.constants import DEFAULT_MODEL
from asxlib.errors import AsxError


class FakeClient:
    base_url = "https://example.test"

    def __init__(self):
        self.submitted = []
        self.downloaded = []
        self.statuses = {}
        self.reject_submit = False
        self.reject_download = False
        self.catalog = []
        buffer = io.BytesIO()
        Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
        self.png = buffer.getvalue()

    def models(self):
        return self.catalog, "catalog"

    def submit(self, body, key):
        if self.reject_submit:
            raise AsxError("connection interrupted", code="network_error")
        self.submitted.append((body, key))
        return {"id": f"task_{len(self.submitted)}", "status": "queued"}, "submit"

    def task(self, task_id):
        status = self.statuses.get(task_id, "succeeded")
        return {
            "id": task_id, "status": status,
            "result": {"assets": [{"index": 0}]},
        }, "poll"

    def asset(self, task_id, index, url, *, metrics):
        if self.reject_download:
            raise AsxError("download interrupted", code="network_error")
        self.downloaded.append(task_id)
        return self.png, "image/png"

    def cancel(self, task_id):
        self.statuses[task_id] = "canceled"
        return self.task(task_id)


def catalog_model(name):
    return {
        "name": name, "task_types": ["image.generate", "image.edit"],
        "capabilities": {
            "image_sizes": ["4K", "2K", "1K"],
            "aspect_ratios": ["16:9", "1:1"],
            "output_formats": ["webp", "png"],
            "max_images": 4, "max_reference_images": 5,
        },
    }


class AuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CACHE_PATH": str(self.root / "cache.json"),
            "ASYNX_UPDATE_CHECK": "off",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = FakeClient()

    def create_batch(self, total=1):
        return batches.create_batch(
            self.client, operation="generate", prompt="cat", model_selector=None,
            image_size=None, aspect_ratio=None, quality=None, count=1,
            output_format=None, references=[], mask=None, total=total, name=None,
            output_dir=str(self.root / "outputs"),
        )["id"]

    def build(self, selector=None, **overrides):
        args = dict(task_type="image.generate", model_selector=selector, prompt="cat",
                    image_size=None, aspect_ratio=None, quality=None, count=1,
                    output_format=None, references=[])
        args.update(overrides)
        return images.build_task(self.client, **args)[0]

    def test_canceled_batch_cannot_be_recovered_or_polled_into_submission(self):
        batch_id = self.create_batch()
        self.client.reject_submit = True
        batches.poll_batch(self.client, batch_id)
        batches.cancel_batch(self.client, batch_id)
        self.assertEqual(tasks.local_tasks_payload()["tasks"][0]["status"], "canceled")
        self.client.reject_submit = False
        tasks.recover_tasks(self.client)
        batches.poll_batch(self.client, batch_id)
        self.assertEqual(self.client.submitted, [])

    def test_recovery_honors_legacy_cancellation_with_stale_ledger(self):
        batch_id = self.create_batch()
        self.client.reject_submit = True
        batches.poll_batch(self.client, batch_id)
        with batches.connect_db() as connection:
            connection.execute("UPDATE batches SET cancel_requested=1 WHERE id=?", (batch_id,))
            connection.execute("UPDATE batch_items SET status='canceled' WHERE batch_id=?", (batch_id,))
        self.client.reject_submit = False
        tasks.recover_tasks(self.client)
        self.assertEqual(self.client.submitted, [])
        self.assertEqual(tasks.local_tasks_payload()["tasks"][0]["status"], "canceled")

    def test_uncanceled_batch_intent_is_still_recoverable(self):
        batch_id = self.create_batch()
        self.client.reject_submit = True
        batches.poll_batch(self.client, batch_id)
        self.client.reject_submit = False
        result = tasks.recover_tasks(self.client)
        self.assertTrue(result["ok"])
        self.assertEqual(len(self.client.submitted), 1)

    def test_missing_files_redownload_for_active_and_completed_batches(self):
        for total in (1, 2):
            with self.subTest(total=total):
                batch_id = self.create_batch(total)
                batches.poll_batch(self.client, batch_id, submissions_limit=1)
                item = batches.batch_status(batch_id, include_items=True)["batch"]["items"][0]
                path = Path(item["files"][0])
                path.unlink()
                self.assertEqual(batches.batch_status(batch_id)["batch"]["pending_download"], 1)
                previous_submissions = len(self.client.submitted)
                previous_downloads = len(self.client.downloaded)
                result = batches.poll_batch(self.client, batch_id)
                self.assertTrue(path.is_file())
                self.assertEqual(result["batch"]["pending_download"], 0)
                self.assertEqual(result["batch"]["status"], "completed")
                self.assertEqual(len(self.client.submitted), previous_submissions + total - 1)
                self.assertEqual(len(self.client.downloaded), previous_downloads + total)

    def test_download_failure_remains_pending_and_wait_retries_without_regeneration(self):
        batch_id = self.create_batch()
        batches.poll_batch(self.client, batch_id)
        item = batches.batch_status(batch_id, include_items=True)["batch"]["items"][0]
        Path(item["files"][0]).unlink()
        self.client.reject_download = True
        result = batches.poll_batch(self.client, batch_id)
        self.assertEqual(result["batch"]["pending_download"], 1)
        self.assertNotIn("finished", result)
        with patch.object(batches.time, "sleep", side_effect=lambda _: setattr(self.client, "reject_download", False)):
            result = batches.wait_for_batch(self.client, batch_id, interval=1, max_wait=5)
        self.assertEqual(result["batch"]["pending_download"], 0)
        self.assertEqual(len(self.client.submitted), 1)

    def test_unsuccessful_terminal_results_remain_failures_on_repeat_and_wait(self):
        for status in ("failed", "timeout", "canceled"):
            with self.subTest(status=status):
                batch_id = self.create_batch(2)
                self.client.statuses[f"task_{len(self.client.submitted) + 1}"] = status
                for result in (
                    batches.poll_batch(self.client, batch_id),
                    batches.poll_batch(self.client, batch_id),
                    batches.wait_for_batch(self.client, batch_id, interval=1, max_wait=1),
                ):
                    self.assertFalse(result["ok"])
                    self.assertTrue(result["batch"]["partial"])
                    self.assertEqual(result["batch"]["counts"][status], 1)
                    self.assertEqual(result["batch"]["counts"]["succeeded"], 1)
                args = argparse.Namespace(batch_command="poll", batch_id=batch_id, limit=4)
                self.assertEqual(batches.execute_batch(self.client, args)[1], 4)
                self.assertTrue(batches.batch_status(batch_id)["ok"])

    def test_catalog_order_does_not_change_defaults(self):
        cold = self.build()
        images.cache_models(self.client.base_url, [catalog_model(DEFAULT_MODEL)])
        warm = self.build()
        self.assertEqual(cold["input"], warm["input"])
        self.assertEqual(warm["input"]["image_size"], "1K")

    def test_fuzzy_model_uses_canonical_model_defaults(self):
        self.client.catalog = [catalog_model("doubao-seedream-5-0-260128")]
        result = self.build("seedream")
        self.assertEqual(result["input"]["image_size"], "2K")
        self.assertEqual(result["input"]["aspect_ratio"], "1:1")
        self.assertEqual(result["input"]["output_format"], "png")

    def test_unsupported_default_is_not_silently_upgraded(self):
        model = catalog_model(DEFAULT_MODEL)
        model["capabilities"]["image_sizes"] = ["4K"]
        images.cache_models(self.client.base_url, [model])
        with self.assertRaises(AsxError):
            self.build()
        self.assertEqual(self.build(image_size="4K")["input"]["image_size"], "4K")

    def test_batch_conversion_preflights_all_collisions_without_writes(self):
        for kind in ("same_stem", "in_place", "existing", "symlink"):
            with self.subTest(kind=kind):
                source = self.root / kind
                source.mkdir()
                Image.new("RGB", (4, 4), "red").save(source / "a.jpg")
                Image.new("RGB", (4, 4), "blue").save(source / "z.jpg")
                destination = self.root / (kind + "_out")
                destination.mkdir()
                if kind == "same_stem":
                    Image.new("RGB", (4, 4), "green").save(source / "z.png")
                elif kind == "in_place":
                    destination = source
                    Image.new("RGB", (4, 4), "green").save(source / "z.png")
                elif kind == "existing":
                    (destination / "z.png").write_bytes(b"keep")
                else:
                    (destination / "z.png").symlink_to(source / "z.jpg")
                originals = {p: p.read_bytes() for p in source.iterdir()}
                outputs = {p: p.read_bytes() for p in destination.iterdir()}
                with self.assertRaises(AsxError) as caught:
                    local_images._batch_convert(str(source), str(destination), "png", False)
                self.assertEqual(caught.exception.code, "image_output_conflict")
                self.assertEqual(originals, {p: p.read_bytes() for p in source.iterdir()})
                self.assertEqual(outputs, {p: p.read_bytes() for p in destination.iterdir()})

    def test_batch_conversion_exclusive_write_preserves_late_created_output(self):
        target = self.root / "late.png"
        target.write_bytes(b"another process")
        with self.assertRaises(AsxError):
            local_images._save(Image.new("RGB", (4, 4)), str(target), "png", exclusive=True)
        self.assertEqual(target.read_bytes(), b"another process")

    def test_recovery_does_not_require_batch_schema(self):
        with state.connect_db() as connection:
            self.assertFalse(state.cancel_blocked_batch_intent(connection, "single-task"))


if __name__ == "__main__":
    unittest.main()
