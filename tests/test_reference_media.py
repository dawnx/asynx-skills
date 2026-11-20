from __future__ import annotations

import base64
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image  # type: ignore[import-not-found]

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "asynx-image" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from asxlib import reference_media, uploads
from asxlib.constants import (
    MAX_MASK_BYTES,
    MAX_REFERENCE_BYTES,
    MAX_REFERENCE_DIMENSION,
    MAX_REFERENCE_IMAGES,
    MAX_REFERENCE_PIXELS,
    MAX_REFERENCE_SOURCE_BYTES,
    MAX_REFERENCE_TOTAL_BYTES,
)
from asxlib.errors import AsxError
from asxlib.media_results import image_mime
from asxlib.reference_media import PreparedImage


class ReferenceMediaTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        environment = patch.dict(
            os.environ, {"ASYNX_CACHE_PATH": str(self.directory / "cache" / "models.json")}
        )
        environment.start()
        self.addCleanup(environment.stop)

    def _image_bytes(
        self,
        *,
        image_format: str = "PNG",
        size: tuple[int, int] = (64, 32),
        mode: str = "RGB",
        color: object = (24, 96, 168),
    ) -> bytes:
        output = io.BytesIO()
        Image.new(mode, size, color).save(output, format=image_format)
        return output.getvalue()

    def _write_image(
        self,
        name: str,
        *,
        image_format: str = "PNG",
        size: tuple[int, int] = (64, 32),
        mode: str = "RGB",
        color: object = (24, 96, 168),
    ) -> tuple[Path, bytes]:
        data = self._image_bytes(
            image_format=image_format,
            size=size,
            mode=mode,
            color=color,
        )
        path = self.directory / name
        path.write_bytes(data)
        return path, data

    @staticmethod
    def _data_url(data: bytes, content_type: str = "image/png") -> str:
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{content_type};base64,{encoded}"

    @staticmethod
    def _staged(token: str) -> tuple[str, bytes]:
        """The type and bytes of the prepared image a request refers to."""
        data = uploads.read(token)
        return image_mime(data), data

    def _assert_error(self, code: str, function: object, *args: object, **kwargs: object) -> AsxError:
        with self.assertRaises(AsxError) as caught:
            function(*args, **kwargs)  # type: ignore[operator]
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_markup_resizes_with_source_and_rejects_misalignment(self):
        with tempfile.TemporaryDirectory() as directory:
            original = Path(directory) / "source.png"
            markup = Path(directory) / "markup.png"
            Image.new("RGB", (4000, 1000), "white").save(original)
            Image.new("RGBA", (4000, 1000), (0, 0, 0, 0)).save(markup)
            result = reference_media.prepare_markup(str(markup), str(original), keep_original=False)
            data = uploads.read(result.source)
            with Image.open(io.BytesIO(data)) as layer:
                self.assertEqual(layer.size, (3840, 960))
            Image.new("RGBA", (1000, 1000)).save(markup)
            with self.assertRaises(AsxError):
                reference_media.prepare_markup(str(markup), str(original), keep_original=False)

    def test_fixed_reference_limits(self) -> None:
        self.assertEqual(MAX_REFERENCE_IMAGES, 16)
        self.assertEqual(MAX_REFERENCE_SOURCE_BYTES, 25 * 1024 * 1024)
        self.assertEqual(MAX_REFERENCE_BYTES, 10 * 1024 * 1024)
        self.assertEqual(MAX_REFERENCE_TOTAL_BYTES, 20 * 1024 * 1024)
        self.assertEqual(MAX_REFERENCE_DIMENSION, 3840)
        self.assertEqual(MAX_REFERENCE_PIXELS, 8_294_400)
        self.assertEqual(MAX_MASK_BYTES, 10 * 1024 * 1024)

    # Re-encoding every round is what blurred an image edited again and again.
    def test_images_within_limits_are_uploaded_unchanged(self) -> None:
        path, png = self._write_image("reference.png")
        sources = (str(path), self._data_url(png))

        for source in sources:
            with self.subTest(source="local" if source == str(path) else "data_url"):
                prepared = reference_media.prepare_image(source)
                content_type, data = self._staged(prepared.source)
                self.assertEqual(data, png)
                self.assertEqual(content_type, "image/png")
                self.assertEqual((prepared.width, prepared.height), (64, 32))
                self.assertFalse(prepared.normalized)

    def test_metadata_rotation_is_applied_without_loss(self) -> None:
        source = Image.new("RGB", (64, 32))
        source.putpixel((0, 0), (255, 0, 0))
        exif = Image.Exif()
        exif[274] = 6
        path = self.directory / "rotated.jpg"
        source.save(path, format="JPEG", exif=exif, quality=100)
        with Image.open(path) as stored:
            upright = stored.transpose(Image.Transpose.ROTATE_270).convert("RGB")

        prepared = reference_media.prepare_image(str(path))

        content_type, data = self._staged(prepared.source)
        self.assertEqual(content_type, "image/webp")
        with Image.open(io.BytesIO(data)) as decoded:
            self.assertEqual(decoded.size, (32, 64))
            self.assertEqual(list(decoded.convert("RGB").getdata()), list(upright.getdata()))

    def test_normalization_scales_to_dimension_and_pixel_limits(self) -> None:
        path, _data = self._write_image("large.png", size=(4000, 2400))

        prepared = reference_media.prepare_image(str(path))

        self.assertIsNotNone(prepared.width)
        self.assertIsNotNone(prepared.height)
        assert prepared.width is not None
        assert prepared.height is not None
        self.assertLessEqual(max(prepared.width, prepared.height), MAX_REFERENCE_DIMENSION)
        self.assertLessEqual(prepared.width * prepared.height, MAX_REFERENCE_PIXELS)
        self.assertLessEqual(prepared.size_bytes or 0, MAX_REFERENCE_BYTES)

    # Lossy encoding is a fallback for images that cannot fit losslessly.
    def test_lossy_encoding_only_when_lossless_does_not_fit(self) -> None:
        noise = Image.effect_noise((512, 512), 64).convert("RGB")
        path = self.directory / "noise.png"
        noise.save(path)
        lossless_size = len(reference_media._webp(
            noise, max_dimension=MAX_REFERENCE_DIMENSION, max_pixels=MAX_REFERENCE_PIXELS, quality=None
        )[0])
        with patch.object(reference_media, "_webp", wraps=reference_media._webp) as encode:
            prepared = reference_media.prepare_image(str(path), max_bytes=lossless_size - 1)
        self.assertEqual([call.kwargs["quality"] for call in encode.call_args_list], [None, 90])
        self.assertEqual((prepared.width, prepared.height), (512, 512))
        self.assertLess(prepared.size_bytes or 0, lossless_size)

        with patch.object(reference_media, "_webp", return_value=(b"x" * 11, 1, 1)) as encode:
            self._assert_error(
                "reference_image_normalization_failed",
                reference_media.prepare_image,
                str(path),
                max_bytes=10,
            )
        self.assertEqual(encode.call_count, 3)

    def test_keep_original_preserves_bytes_and_still_enforces_delivery_limits(self) -> None:
        path, jpeg = self._write_image("reference.jpg", image_format="JPEG")

        prepared = reference_media.prepare_image(str(path), keep_original=True)

        content_type, data = self._staged(prepared.source)
        self.assertEqual(content_type, "image/jpeg")
        self.assertEqual(data, jpeg)
        self.assertEqual(prepared.content_type, "image/jpeg")
        self.assertFalse(prepared.normalized)

        oversized, _data = self._write_image("oversized.png", size=(65, 32))
        with patch.object(reference_media, "MAX_REFERENCE_DIMENSION", 64):
            self._assert_error(
                "reference_image_requires_normalization",
                reference_media.prepare_image,
                str(oversized),
                keep_original=True,
            )

    def test_corrupt_animated_and_unsupported_images_are_rejected(self) -> None:
        corrupt = self.directory / "corrupt.png"
        corrupt.write_bytes(b"not an image")
        self._assert_error("invalid_image_content", reference_media.prepare_image, str(corrupt))

        animated = self.directory / "animated.webp"
        frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
        frames[0].save(animated, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
        self._assert_error(
            "animated_image_not_supported", reference_media.prepare_image, str(animated)
        )

        bitmap, _data = self._write_image("unsupported.bmp", image_format="BMP")
        self._assert_error("unsupported_image_format", reference_media.prepare_image, str(bitmap))

    def test_source_larger_than_25_mb_is_rejected_before_decode(self) -> None:
        oversized = self.directory / "oversized.png"
        with oversized.open("wb") as file:
            file.seek(MAX_REFERENCE_SOURCE_BYTES)
            file.write(b"\0")

        self._assert_error("reference_image_too_large", reference_media.prepare_image, str(oversized))

    def test_at_most_five_reference_images_are_accepted(self) -> None:
        references = [f"https://images.example/{index}.png" for index in range(17)]

        prepared, mask = reference_media.prepare_inputs(
            references[:MAX_REFERENCE_IMAGES],
            mask=None,
            keep_original=False,
        )
        self.assertEqual(prepared, references[:MAX_REFERENCE_IMAGES])
        self.assertIsNone(mask)

        self._assert_error(
            "too_many_images",
            reference_media.prepare_inputs,
            references,
            mask=None,
            keep_original=False,
        )

    def test_combined_local_input_limit_counts_references_and_mask(self) -> None:
        references = ["first.png", "second.png"]
        prepared = [
            PreparedImage("first", "image/webp", 9, 1, 1, True),
            PreparedImage("second", "image/webp", 9, 1, 1, True),
        ]
        mask = PreparedImage("mask", "image/png", 3, 1, 1, False)
        mask_path = self.directory / "mask.png"
        mask_path.write_bytes(b"abc")
        with (
            patch.object(reference_media, "MAX_REFERENCE_TOTAL_BYTES", 20),
            patch.object(reference_media, "prepare_image", side_effect=prepared) as prepare,
            patch.object(reference_media, "prepare_mask", return_value=mask),
        ):
            self._assert_error(
                "reference_images_total_too_large",
                reference_media.prepare_inputs,
                references,
                mask=str(mask_path),
                keep_original=False,
            )
        # Originals that fit alone but not together get re-encoded, not refused.
        self.assertEqual({call.kwargs["max_bytes"] for call in prepare.call_args_list}, {8})

    def test_valid_mask_is_preserved_as_png(self) -> None:
        mask_image = Image.new("RGBA", (2, 1), (0, 0, 0, 0))
        mask_image.putpixel((1, 0), (0, 0, 0, 255))
        mask = self.directory / "mask.png"
        mask_image.save(mask, format="PNG")
        original = mask.read_bytes()
        source = PreparedImage("source", "image/webp", 10, 2, 1, True)

        prepared = reference_media.prepare_mask(str(mask), source=source)

        content_type, data = self._staged(prepared.source)
        self.assertEqual(content_type, "image/png")
        self.assertEqual(data, original)
        self.assertEqual((prepared.width, prepared.height), (2, 1))
        self.assertFalse(prepared.normalized)

    def test_mask_must_be_single_frame_png_with_alpha_extremes(self) -> None:
        jpeg, _data = self._write_image("mask.jpg", image_format="JPEG")
        self._assert_error("invalid_image_mask", reference_media.prepare_mask, str(jpeg), source=None)

        opaque, _data = self._write_image("opaque.png")
        self._assert_error("invalid_image_mask", reference_media.prepare_mask, str(opaque), source=None)

        partial, _data = self._write_image(
            "partial.png", mode="RGBA", color=(0, 0, 0, 128), size=(2, 1)
        )
        self._assert_error("invalid_image_mask", reference_media.prepare_mask, str(partial), source=None)

        animated = self.directory / "mask-animated.webp"
        frames = [Image.new("RGBA", (2, 1), (0, 0, 0, alpha)) for alpha in (0, 255)]
        frames[0].save(animated, format="WEBP", save_all=True, append_images=frames[1:], duration=100)
        self._assert_error(
            "animated_image_not_supported", reference_media.prepare_mask, str(animated), source=None
        )

    def test_mask_size_dimensions_and_aspect_ratio_are_limited(self) -> None:
        oversized = self.directory / "oversized-mask.png"
        with oversized.open("wb") as file:
            file.seek(MAX_MASK_BYTES)
            file.write(b"\0")
        self._assert_error(
            "reference_image_too_large", reference_media.prepare_mask, str(oversized), source=None
        )

        mask_image = Image.new("RGBA", (4, 2), (0, 0, 0, 0))
        mask_image.putpixel((3, 1), (0, 0, 0, 255))
        mask = self.directory / "mask.png"
        mask_image.save(mask, format="PNG")

        smaller_source = PreparedImage("source", "image/webp", 10, 3, 2, True)
        self._assert_error(
            "invalid_image_mask", reference_media.prepare_mask, str(mask), source=smaller_source
        )

        wrong_aspect_source = PreparedImage("source", "image/webp", 10, 4, 4, True)
        self._assert_error(
            "invalid_image_mask", reference_media.prepare_mask, str(mask), source=wrong_aspect_source
        )


if __name__ == "__main__":
    unittest.main()
