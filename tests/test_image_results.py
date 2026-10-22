from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib.image_results import attach_image_details
from asxlib.tasks import task_payload


class ImageResultsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def save_image(self, name="image.png", size=(600, 900), **options):
        path = self.root / name
        Image.new("RGB", size, "red").save(path, **options)
        return str(path)

    def test_reports_local_dimensions_and_format_in_file_order(self):
        files = [self.save_image(name) for name in ("a.png", "b.jpg", "c.webp")]
        payload = task_payload(
            {"status": "succeeded", "result": {"assets": [{"width": 1, "height": 1}]}},
            None, files=files, expected_ratio="2:3",
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["files"], files)
        self.assertEqual([item["path"] for item in payload["images"]], files)
        self.assertEqual([item["format"] for item in payload["images"]], ["png", "jpeg", "webp"])
        for item in payload["images"]:
            self.assertEqual((item["width"], item["height"]), (600, 900))
            self.assertEqual(item["aspect_ratio"], "2:3")
            self.assertEqual(item["size_bytes"], Path(item["path"]).stat().st_size)
        self.assertNotIn("warnings", payload)

    def test_warns_on_wrong_ratio_without_discarding_download(self):
        path = self.save_image(size=(512, 512))
        payload = task_payload({"status": "succeeded"}, None, files=[path], expected_ratio="2:3")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["files"], [path])
        self.assertEqual(payload["warnings"][0]["code"], "aspect_ratio_mismatch")
        self.assertEqual(payload["warnings"][0]["actual_aspect_ratio"], "1:1")
        self.assertEqual(payload["warnings"][0]["requested_aspect_ratio"], "2:3")

    def test_accepts_pixel_rounding_without_false_mismatch(self):
        payload = {}
        attach_image_details(payload, [self.save_image(size=(1024, 683))], expected_ratio="3:2")
        self.assertNotIn("warnings", payload)

    def test_pixel_rounding_does_not_hide_large_ratio_error_on_tiny_image(self):
        payload = {}
        attach_image_details(payload, [self.save_image(size=(1, 1))], expected_ratio="2:3")
        self.assertEqual(payload["warnings"][0]["code"], "aspect_ratio_mismatch")

    def test_uses_exif_display_orientation(self):
        exif = Image.Exif()
        exif[274] = 6
        path = self.save_image("rotated.jpg", size=(900, 600), exif=exif)
        payload = {}
        attach_image_details(payload, [path], expected_ratio="2:3")
        self.assertEqual((payload["images"][0]["width"], payload["images"][0]["height"]), (600, 900))
        self.assertNotIn("warnings", payload)

    def test_missing_and_invalid_files_do_not_hide_good_results_or_prior_warnings(self):
        valid = self.save_image()
        invalid = self.root / "invalid.png"
        invalid.write_bytes(b"not an image")
        previous = {"code": "previous_warning"}
        payload = {"warnings": [previous]}
        attach_image_details(payload, [str(invalid), str(self.root / "missing.png"), valid])
        self.assertEqual([item["path"] for item in payload["images"]], [valid])
        self.assertEqual(payload["warnings"][0], previous)
        self.assertEqual([item["code"] for item in payload["warnings"][1:]], ["image_metadata_unavailable"] * 2)

    def test_missing_pillow_does_not_turn_success_into_failure(self):
        path = self.save_image()
        with patch("asxlib.image_results.importlib.import_module", side_effect=ImportError):
            payload = task_payload({"status": "succeeded"}, None, files=[path])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["files"], [path])
        self.assertEqual(payload["warnings"][0]["code"], "image_metadata_unavailable")

    def test_pixel_limit_produces_warning_without_loading_image(self):
        path = self.save_image(size=(20, 20))
        for limit in (300, 100):
            with self.subTest(limit=limit), patch.object(Image, "MAX_IMAGE_PIXELS", limit):
                payload = task_payload({"status": "succeeded"}, None, files=[path])
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["warnings"][0]["code"], "image_metadata_unavailable")

    def test_remote_only_result_has_no_local_metadata(self):
        payload = task_payload({"status": "queued"}, None)
        self.assertNotIn("images", payload)
        self.assertNotIn("warnings", payload)

    def test_remote_wait_can_use_request_ratio_from_task(self):
        path = self.save_image()
        payload = task_payload(
            {"status": "succeeded", "input": {"aspect_ratio": "1:1"}}, None, files=[path],
        )
        self.assertEqual(payload["warnings"][0]["requested_aspect_ratio"], "1:1")


if __name__ == "__main__":
    unittest.main()
