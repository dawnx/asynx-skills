from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))
from asxlib.cli import parser
from asxlib.errors import AsxError
from asxlib.image_options import options_from_args, resolved_image_options
from asxlib.images import build_task


class PromptOptimizationTests(unittest.TestCase):
    def test_cli_mode_reaches_actual_request_and_does_not_rewrite_explicit_parameters(self):
        args = parser().parse_args(["generate", "--prompt", "Keep my text", "--model", "doubao-seedream-5-0-pro", "--image-size", "1.5K", "--optimize-prompt-mode", "fast"])
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"ASYNX_CACHE_PATH": str(Path(temporary) / "catalog.json")}):
            client = Namespace(base_url="http://127.0.0.1:1", cache_scope="test-only")
            body, _, _ = build_task(client, task_type="image.generate", model_selector=args.model,
                prompt=args.prompt, image_size=args.image_size, aspect_ratio=None, quality=None,
                count=1, output_format="png", references=[], options=options_from_args(args))
        self.assertEqual(body["input"]["extra"], {"optimize_prompt_options": {"mode": "fast"}})
        self.assertEqual(body["input"]["prompt"], "Keep my text")
        self.assertEqual(body["input"]["image_size"], "1.5K")
        self.assertEqual(body["input"]["count"], 1)

    def test_parameter_file_is_controlled_and_cli_override_survives_batch_inheritance(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "params.json"
            path.write_text(json.dumps({"optimize_prompt_options": {"mode": "standard"}}))
            options = options_from_args(Namespace(params_file=str(path), optimize_prompt_mode="fast"))
            inherited = {"extra": {"optimize_prompt_options": {"mode": "standard"}}}
            resolved = resolved_image_options("png", {**inherited, **options}, image_size=None, aspect_ratio=None)
            self.assertEqual(resolved["extra"]["optimize_prompt_options"]["mode"], "fast")
            self.assertEqual(resolved_image_options("png", resolved, image_size=None, aspect_ratio=None), resolved)
            for invalid in [{"extra": {"headers": {"Authorization": "secret"}}}, {"optimize_prompt_options": {"mode": "fast", "model": "other"}}, {"optimize_prompt_options": {"mode": "invented"}}]:
                path.write_text(json.dumps(invalid))
                with self.subTest(invalid=invalid), self.assertRaises(AsxError):
                    options_from_args(Namespace(params_file=str(path)))

    def test_conflicting_or_arbitrary_internal_extra_is_rejected_before_submission(self):
        for options in [
            {"extra": {"model": "other"}},
            {"optimize_prompt_options": {"mode": "fast"}, "extra": {"optimize_prompt_options": {"mode": "standard"}}},
        ]:
            with self.subTest(options=options), self.assertRaises(AsxError):
                resolved_image_options("png", options, image_size=None, aspect_ratio=None)
