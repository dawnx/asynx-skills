from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))
from asxlib import uploads, videos
from asxlib.errors import AsxError


class VideoBuilderTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        environment = patch.dict(os.environ, {"ASYNX_CACHE_PATH": str(self.root / "cache" / "models.json")})
        environment.start()
        self.addCleanup(environment.stop)
        self.client = Mock()

    def source(self, name="input.png", size=(40, 30), alpha=False, orientation=None):
        source = Image.new("RGBA" if alpha else "RGB", size, (20, 80, 120, 128) if alpha else (20, 80, 120))
        exif = Image.Exif()
        if orientation:
            exif[274] = orientation
        path = self.root / name
        source.save(path, exif=exif)
        return str(path)

    def build(self, **kwargs):
        return videos.build_video_task(self.client, prompt="镜头缓慢靠近，伴随鸟鸣声", **kwargs)[0]

    def test_default_public_contract_preserves_sound_prompt_without_switch(self):
        body = self.build()
        self.assertEqual(body["task_type"], "video.generate")
        # Which video model runs is the service's configured default.
        self.assertNotIn("model", body)
        self.assertEqual(body["asset_delivery"], "asynx")
        self.assertEqual(body["input"]["duration"], 5)
        self.assertEqual(body["input"]["resolution"], "720p")
        self.assertIn("鸟鸣声", body["input"]["prompt"])
        self.assertNotIn("generate_audio", body["input"])
        self.assertEqual(body["input"]["inputs"], [])
        self.client.models.assert_not_called()

    def test_user_duration_is_not_rounded_or_clamped(self):
        for value in (2, 31, 7.25, 0, -1):
            with self.subTest(value=value):
                self.assertEqual(self.build(duration=value)["input"]["duration"], value)
        for value in (True, float("nan"), float("inf"), "5"):
            with self.subTest(value=value), self.assertRaises(AsxError):
                self.build(duration=value)

    def test_named_video_model_is_sent_as_given_and_768p_is_rejected_before_network(self):
        # The Skill used to refuse every video model but one hard-coded name.
        self.assertEqual(self.build(model_selector="seedance-2-0")["model"], "seedance-2-0")
        with self.assertRaises(AsxError):
            self.build(resolution="768p")
        self.client.assert_not_called()

    def test_image_inputs_are_oriented_scaled_jpeg_or_png_not_webp(self):
        source = self.source(size=(2000, 1000), orientation=6)
        body = self.build(first_frame=source)
        item = body["input"]["inputs"][0]
        self.assertEqual(item["role"], "first_frame")
        with Image.open(io.BytesIO(uploads.read(item["url"]))) as image:
            self.assertEqual((image.format, image.size), ("JPEG", (800, 1600)))
        alpha = self.source(name="alpha.png", alpha=True)
        with Image.open(io.BytesIO(uploads.read(self.build(references=[alpha])["input"]["inputs"][0]["url"]))) as image:
            self.assertEqual(image.format, "PNG")

    def test_data_url_is_normalized_and_single_pass_output_limit_is_enforced(self):
        source = self.source()
        data_url = "data:image/png;base64," + base64.b64encode(Path(source).read_bytes()).decode()
        with Image.open(io.BytesIO(uploads.read(self.build(references=[data_url])["input"]["inputs"][0]["url"]))) as image:
            self.assertEqual(image.format, "JPEG")
        with patch.object(videos, "_decoded_image", return_value=(Image.new("RGB", (2, 2)), "image/png", 1)), patch.object(videos.io, "BytesIO") as buffer, patch.object(Image.Image, "save"):
            buffer.return_value.getvalue.return_value = b"x" * (3 * 1024 * 1024 + 1)
            with self.assertRaises(AsxError) as caught:
                self.build(references=[source])
        self.assertEqual(caught.exception.code, "video_reference_too_large")

    def test_roles_are_explicit_and_tail_requires_start(self):
        first, last, ref = [f"https://example.test/{name}.png" for name in ("start", "end", "ref")]
        inputs = self.build(first_frame=first, last_frame=last, references=[ref],
                            reference_videos=["https://example.test/ref.mp4"],
                            reference_audios=["https://example.test/ref.mp3"])["input"]["inputs"]
        self.assertEqual([(i["type"], i["role"]) for i in inputs], [
            ("image", "reference"), ("image", "first_frame"), ("image", "last_frame"),
            ("video", "reference"), ("audio", "reference"),
        ])
        with self.assertRaises(AsxError) as caught:
            self.build(last_frame=last)
        self.assertEqual(caught.exception.code, "missing_first_frame")

    def test_local_video_audio_are_rejected_instead_of_leaking_paths(self):
        for field in ("reference_videos", "reference_audios"):
            with self.subTest(field=field), self.assertRaises(AsxError) as caught:
                self.build(**{field: [str(self.root / "private.mp4")]})
            self.assertEqual(caught.exception.code, "video_reference_url_required")

    def test_args_bridge_preserves_user_values(self):
        body, model, request_id = videos.body_from_video_args(self.client, argparse.Namespace(
            prompt="test", duration=6.5, resolution="1080p", aspect_ratio="9:16", reference=[],
        ))
        self.assertEqual(model, "default")
        self.assertIsNone(request_id)
        self.assertEqual(body["input"]["duration"], 6.5)
        self.assertEqual(body["input"]["aspect_ratio"], "9:16")

    def test_optional_cli_defaults_are_applied_without_changing_explicit_zero(self):
        for duration in (None, 0, -1, 5.5):
            args = argparse.Namespace(prompt="test", duration=duration, resolution=None)
            body, _model, _rid = videos.body_from_video_args(self.client, args)
            self.assertEqual(body["input"]["duration"], 5 if duration is None else duration)
            self.assertEqual(body["input"]["resolution"], "720p")


if __name__ == "__main__":
    unittest.main()
