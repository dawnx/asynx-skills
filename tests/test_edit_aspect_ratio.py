from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import images, reference_media
from asxlib.constants import DEFAULT_MODEL
from asxlib.errors import AsxError
from test_audit_regressions import FakeClient, catalog_model


class EditAspectRatioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"ASYNX_CACHE_PATH": str(self.root / "models.json")})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = FakeClient()

    def image(self, name, size, *, orientation=None):
        path = self.root / name
        source = Image.new("RGB", size, "red")
        exif = Image.Exif()
        if orientation:
            exif[274] = orientation
        source.save(path, exif=exif)
        return str(path)

    def build(self, references, **overrides):
        options = {
            "task_type": "image.edit", "model_selector": None, "prompt": "换成蓝色",
            "image_size": None, "aspect_ratio": None, "quality": None, "count": 1,
            "output_format": None, "references": references,
        }
        options.update(overrides)
        return images.build_task(self.client, **options)[0]

    def test_edit_preserves_original_ratio_before_resize_without_catalog_request(self):
        path = self.image("large.png", (3000, 2000))
        with (
            patch.object(self.client, "models", side_effect=AssertionError("unexpected catalog request")),
            patch.object(reference_media, "_webp", wraps=reference_media._webp) as encode,
        ):
            body = self.build([path])
        self.assertEqual(body["input"]["aspect_ratio"], "3:2")
        self.assertEqual(encode.call_count, 1)

    def test_exif_display_orientation_is_used(self):
        path = self.image("rotated.jpg", (180, 120), orientation=6)
        self.assertEqual(self.build([path])["input"]["aspect_ratio"], "2:3")

    def test_data_url_and_original_encoding_also_inherit_ratio(self):
        path = self.image("source.png", (120, 180))
        url = "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()
        for source in (path, url):
            for keep in (False, True):
                with self.subTest(source=source[:20], keep=keep):
                    self.assertEqual(
                        self.build([source], keep_reference_original=keep)["input"]["aspect_ratio"], "2:3"
                    )

    def test_explicit_ratio_overrides_input(self):
        source = self.image("portrait.png", (120, 180))
        self.assertEqual(self.build([source], aspect_ratio="16:9")["input"]["aspect_ratio"], "16:9")

    def test_generation_with_reference_keeps_generation_default(self):
        source = self.image("portrait.png", (120, 180))
        self.assertEqual(self.build([source], task_type="image.generate")["input"]["aspect_ratio"], "1:1")

    def test_equal_ratio_multiple_images_can_inherit(self):
        sources = [self.image("a.png", (60, 90)), self.image("b.png", (120, 180))]
        self.assertEqual(self.build(sources)["input"]["aspect_ratio"], "2:3")

    def test_mixed_ratios_require_explicit_choice(self):
        sources = [self.image("a.png", (60, 90)), self.image("b.png", (90, 60))]
        with self.assertRaises(AsxError) as caught:
            self.build(sources)
        self.assertEqual(caught.exception.code, "ambiguous_edit_aspect_ratio")
        self.assertEqual(self.build(sources, aspect_ratio="1:1")["input"]["aspect_ratio"], "1:1")

    def test_url_edit_requires_ratio_without_downloading_reference(self):
        sources = ["https://example.test/image.png"]
        with self.assertRaises(AsxError) as caught:
            self.build(sources)
        self.assertEqual(caught.exception.code, "edit_aspect_ratio_required")
        self.assertEqual(self.build(sources, aspect_ratio="2:3")["input"]["aspect_ratio"], "2:3")

    def test_unsupported_inherited_ratio_is_not_silently_replaced(self):
        images.cache_models(self.client.base_url, [catalog_model(DEFAULT_MODEL)])
        source = self.image("portrait.png", (120, 180))
        with self.assertRaises(AsxError) as caught:
            self.build([source])
        self.assertEqual(caught.exception.code, "unsupported_model_capability")
        self.assertEqual(caught.exception.details["requested"], "2:3")
        self.assertEqual(self.build([source], aspect_ratio="1:1")["input"]["aspect_ratio"], "1:1")


if __name__ == "__main__":
    unittest.main()
