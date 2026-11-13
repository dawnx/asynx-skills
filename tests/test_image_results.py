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

    def test_svg_and_animation_keep_original_bytes_and_metadata(self):
        from asxlib.errors import AsxError
        from asxlib.media_results import image_mime, media_file_info
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"><path d="M0 0"/></svg>'
        vector = self.root / "result.svg"
        vector.write_bytes(svg)
        animated = self.root / "result.gif"
        Image.new("RGB", (10, 10), "red").save(animated, save_all=True, append_images=[Image.new("RGB", (10, 10), "blue")], duration=[100, 250], loop=0)
        before = animated.read_bytes()
        self.assertEqual(image_mime(svg), "image/svg+xml")
        self.assertEqual(media_file_info(animated)["extension"], "gif")
        payload = {}
        attach_image_details(payload, [str(vector), str(animated)])
        self.assertEqual(payload["images"][0]["format"], "svg")
        self.assertEqual(payload["images"][1]["frame_count"], 2)
        self.assertEqual(payload["images"][1]["duration_ms"], 350)
        self.assertEqual(animated.read_bytes(), before)
        self.assertEqual(vector.read_bytes(), svg)
        with self.assertRaises(AsxError):
            image_mime(b'<svg><script>alert(1)</script></svg>')

    def test_svg_delivery_accepts_internal_references_and_rejects_active_content(self):
        # A paid SVG result must download with quoted fragment URLs, without allowing remote loads.
        from asxlib.errors import AsxError
        from asxlib.tasks import download_assets

        class Client:
            data = b""

            def asset(self, *args, **kwargs):
                return self.data, "image/svg+xml"

        client = Client()
        task = {"id": "svg_task", "task_type": "image.generate", "result": {"assets": [{"index": 0}]}}
        for fill in ("url(#g)", "url(&apos;#g&apos;)", "url(&quot;#g&quot;)", "url( #g )"):
            with self.subTest(fill=fill):
                client.data = (
                    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
                    '<defs><linearGradient id="g"/></defs>'
                    f'<rect width="200" height="100" fill="{fill}"/></svg>'
                ).encode()
                files = download_assets(client, task, str(self.root))
                self.assertEqual(Path(files[0]).suffix, ".svg")
                self.assertEqual(Path(files[0]).read_bytes(), client.data)
        unsafe = (
            '<svg><rect fill="url( https://example.test/image )"/></svg>',
            '<svg><style>rect {fill: url (\n//example.test/image\n)}</style></svg>',
            '<svg><style>rect {fill: url(//example.test/image}</style></svg>',
            '<svg><rect style="fill: u\\72l(https://example.test/image)"/></svg>',
            '<svg><style>@import "https://example.test/style";</style></svg>',
            '<svg><script>alert(1)</script></svg>',
            '<svg onload="alert(1)"/>',
            '<svg xmlns:x="urn:other"><x:script/></svg>',
            '<svg><image href="data:image/png;base64,AA=="/></svg>',
            '<!DOCTYPE svg [<!ENTITY x "expanded">]><svg><text>&x;</text></svg>',
        )
        from asxlib.svg_results import svg_details
        for value in unsafe:
            with self.subTest(value=value), self.assertRaises(AsxError):
                svg_details(value.encode())

    def test_svg_viewbox_does_not_invent_pixel_dimensions(self):
        from asxlib.svg_results import svg_details
        responsive = svg_details(b'<svg xmlns="http://www.w3.org/2000/svg" width="100%" viewBox="-10 0 200.5 100.25"/>')
        self.assertEqual(responsive["view_box"], [-10, 0, 200.5, 100.25])
        self.assertEqual(responsive["view_box_aspect_ratio"], "2:1")
        self.assertNotIn("width", responsive)
        self.assertNotIn("height", responsive)
        explicit = svg_details(b'<svg width="200.5px" height="100.25"/>')
        self.assertEqual(explicit["width"], 200.5)
        self.assertEqual(explicit["height"], 100.25)
        self.assertEqual(explicit["aspect_ratio"], "2:1")

    def test_animation_inspection_budget_preserves_download(self):
        # Unbounded animations must not block delivery or consume unlimited decoding work.
        path = self.root / "many-frames.gif"
        frames = [Image.new("RGB", (2, 2), (index, 0, 0)) for index in range(65)]
        frames[0].save(path, save_all=True, append_images=frames[1:], duration=100)
        original = path.read_bytes()
        payload = task_payload({"status": "succeeded"}, None, files=[str(path)])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["files"], [str(path)])
        self.assertNotIn("duration_ms", payload)
        self.assertEqual(payload["images"], [])
        self.assertEqual(payload["warnings"][0]["code"], "image_metadata_unavailable")
        self.assertEqual(path.read_bytes(), original)

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
