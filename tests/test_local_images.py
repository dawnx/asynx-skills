from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image  # type: ignore[import-not-found]

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "asx" / "scripts"
CLI = SCRIPTS / "asynx.py"
sys.path.insert(0, str(SCRIPTS))

from asxlib import local_images
from asxlib.errors import AsxError


class LocalImagesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.png"
        image = Image.new("RGBA", (100, 60), (20, 80, 160, 255))
        image.save(self.source)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_info_and_conversion_are_local_only(self) -> None:
        info = local_images._image_info(str(self.source))
        self.assertEqual(info["width"], 100)
        self.assertEqual(info["height"], 60)
        self.assertTrue(info["has_alpha"])

        output = self.root / "converted.jpg"
        result = local_images._save(local_images._open_image(str(self.source))[0], str(output), "jpeg")
        self.assertEqual(result["format"], "jpeg")
        with Image.open(output) as converted:
            self.assertEqual(converted.format, "JPEG")
            self.assertEqual(converted.mode, "RGB")

    def test_resize_crop_and_slice(self) -> None:
        resized = local_images._resize(str(self.source), str(self.root / "resize.png"), 50, None, None)
        self.assertEqual((resized["width"], resized["height"]), (50, 30))

        cropped = local_images._crop(
            str(self.source), str(self.root / "crop.png"), "10,5,60,40", None
        )
        self.assertEqual((cropped["width"], cropped["height"]), (50, 35))

        sliced = local_images._slice(str(self.source), str(self.root / "tiles"), 2, 3, "tile", "png")
        self.assertEqual(sliced["count"], 6)
        self.assertTrue(all(Path(item["path"]).is_file() for item in sliced["files"]))

    def test_contact_sheet_and_mask(self) -> None:
        second = self.root / "second.png"
        Image.new("RGB", (40, 80), (200, 40, 20)).save(second)
        sheet = local_images._contact_sheet(
            [str(self.source), str(second)],
            str(self.root / "sheet.png"),
            2,
            100,
            100,
            "white",
            "png",
        )
        self.assertEqual((sheet["width"], sheet["height"]), (200, 100))

        mask = self.root / "mask.png"
        mask_image = Image.new("L", (100, 60), 0)
        mask_image.paste(255, (0, 0, 50, 60))
        mask_image.save(mask)
        result = local_images._apply_mask(
            str(self.source), str(mask), str(self.root / "masked.png"), "png"
        )
        with Image.open(result["path"]) as masked:
            self.assertEqual(masked.getpixel((10, 10))[3], 255)
            self.assertEqual(masked.getpixel((90, 10))[3], 0)

    def test_batch_convert_reports_each_file(self) -> None:
        source_dir = self.root / "inputs"
        source_dir.mkdir()
        self.source.rename(source_dir / "one.png")
        Image.new("RGB", (8, 8), "red").save(source_dir / "two.jpg")
        result = local_images._batch_convert(
            str(source_dir), str(self.root / "outputs"), "webp", recursive=False
        )
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["failed_count"], 0)
        self.assertTrue(all(Path(item["path"]).suffix == ".webp" for item in result["files"]))

    def test_local_cli_does_not_require_credentials_or_state_database(self) -> None:
        state_path = self.root / "should-not-exist.db"
        environment = os.environ.copy()
        environment.update(
            {
                "HOME": str(self.root),
                "ASYNX_STATE_PATH": str(state_path),
                "ASYNX_CONFIG_PATH": str(self.root / "config.json"),
            }
        )
        result = subprocess.run(
            [sys.executable, str(CLI), "image", "info", str(self.source)],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["image"]["width"], 100)
        self.assertFalse(state_path.exists())

    def test_invalid_crop_and_remote_input_are_rejected(self) -> None:
        with self.assertRaises(AsxError) as crop_error:
            local_images._crop(str(self.source), str(self.root / "crop.png"), "0,0,101,60", None)
        self.assertEqual(crop_error.exception.code, "invalid_crop_box")
        with self.assertRaises(AsxError) as remote_error:
            local_images._image_info("https://example.com/image.png")
        self.assertEqual(remote_error.exception.code, "local_image_requires_path")


if __name__ == "__main__":
    unittest.main()
