from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

import test_cli_e2e as image_e2e
from asxlib import cli, models3d
from asxlib.errors import AsxError
from mock_asynx_server import GLB, MODEL_3D

GLB_OUTPUT = {"formats": ["glb"]}


class CatalogClient:
    base_url = "https://contract.test"
    cache_scope = "d" * 64

    def __init__(self, *names):
        self.names = names
        self.catalog_requests = 0

    def models(self):
        self.catalog_requests += 1
        return [{**MODEL_3D, "name": name} for name in self.names], "catalog"


class ModelRequestBodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {"ASYNX_CACHE_PATH": str(self.root / "models.json")})
        env.start()
        self.addCleanup(env.stop)
        self.client = CatalogClient("tripo-h3.1")

    def body(self, *flags):
        args = cli.parser().parse_args(["model", "generate", *flags])
        return models3d.body_from_model_args(self.client, args)[0]

    def test_text_mode_body_carries_only_the_fields_the_service_accepts(self):
        self.assertEqual(self.body("--prompt", "一只陶瓷茶壶"), {
            "task_type": "model.generate", "model": "tripo-h3.1", "asset_delivery": "asynx",
            "input": {"mode": "text", "prompt": "一只陶瓷茶壶", "output": GLB_OUTPUT},
        })
        detailed = self.body(
            "--prompt", "一只陶瓷茶壶", "--negative-prompt", "裂纹", "--no-pbr",
            "--max-faces", "50000", "--seed", "3", "--texture-seed", "7", "--model", "tripo-h3.1",
        )
        self.assertEqual(detailed["input"], {
            "mode": "text", "prompt": "一只陶瓷茶壶", "negative_prompt": "裂纹",
            "texture": {"pbr": False, "seed": 7}, "geometry": {"max_faces": 50000, "seed": 3},
            "output": GLB_OUTPUT,
        })
        # Geometry only: pbr stays unset so the service never sees pbr=true without a texture.
        self.assertEqual(self.body("--prompt", "茶壶", "--no-texture")["input"]["texture"], {"enabled": False})

    def test_image_mode_never_sends_prompt_fields(self):
        url = "https://example.test/teapot.png"
        self.assertEqual(self.body("--image", url), {
            "task_type": "model.generate", "model": "tripo-h3.1", "asset_delivery": "asynx",
            "input": {"mode": "image", "image": url, "output": GLB_OUTPUT},
        })
        source = self.root / "teapot.png"
        Image.new("RGB", (8, 6), "white").save(source)
        local = self.body("--image", str(source), "--max-faces", "20000")["input"]
        self.assertEqual(set(local), {"mode", "image", "geometry", "output"})
        self.assertTrue(local["image"].startswith("data:image/webp;base64,"))
        requests_before = self.client.catalog_requests
        for flags in (
            ["--image", url, "--prompt", "茶壶"], ["--image", url, "--negative-prompt", "裂纹"],
            ["--negative-prompt", "裂纹"], [],
        ):
            with self.subTest(flags=flags), self.assertRaises(AsxError) as caught:
                self.body(*flags)
            self.assertIn(caught.exception.code, {"conflicting_model_inputs", "missing_model_input"})
        self.assertEqual(self.client.catalog_requests, requests_before)

    def test_omitted_model_is_not_guessed_when_several_3d_models_are_available(self):
        self.client = CatalogClient("tripo-h3.1", "tripo-v3.0")
        with self.assertRaises(AsxError) as caught:
            self.body("--prompt", "茶壶")
        self.assertEqual(caught.exception.code, "model_required")
        self.assertIn("models --modality model", caught.exception.message)
        self.assertEqual(self.body("--prompt", "茶壶", "--model", "tripo-v3.0")["model"], "tripo-v3.0")


class ModelCLITests(unittest.TestCase):
    setUp = image_e2e.CLIE2ETestCase.setUp
    tearDown = image_e2e.CLIE2ETestCase.tearDown
    run_cli = image_e2e.CLIE2ETestCase.run_cli

    def submissions(self):
        with urlopen(self.base_url + "/__mock__/stats", timeout=5) as response:
            return json.load(response)["data"]["submissions"]

    def test_killed_wait_resumes_the_original_model_without_a_second_generation(self):
        output_dir = self.temp_path / "model"
        process = subprocess.Popen(
            [sys.executable, str(image_e2e.CLI), "model", "generate", "--prompt", "一只陶瓷茶壶",
             "--output-dir", str(output_dir)],
            cwd=image_e2e.ROOT, env=self.environment, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        watchdog = threading.Timer(30, process.kill)
        watchdog.start()
        try:
            assert process.stderr is not None
            # Stop the command the way a host would, while it is polling the running task.
            polling = any("生成中" in line for line in process.stderr)
        finally:
            process.kill()
            process.communicate(timeout=10)
            watchdog.cancel()
        self.assertTrue(polling, "the command ended before it was waiting on a running task")
        self.assertEqual(list(output_dir.glob("*")), [])

        local = self.run_cli("task", "list", with_credentials=False)["tasks"][0]
        self.assertEqual(local["task_type"], "model.generate")
        resumed = self.run_cli("wait", local["local_id"])
        self.assertTrue(resumed["ok"])
        self.assertEqual(resumed["task_id"], local["task_id"])
        model_path, preview_path = (Path(value) for value in resumed["files"])
        self.assertEqual([item["path"] for item in resumed["models"]], [str(model_path)])
        self.assertEqual([item["path"] for item in resumed["images"]], [str(preview_path)])
        self.assertTrue(model_path.is_absolute() and preview_path.is_absolute())
        self.assertEqual(model_path.suffix, ".glb")
        self.assertEqual(model_path.read_bytes(), GLB)
        self.assertEqual(self.submissions(), 1)
        recent = self.run_cli("recent", "--modality", "model", with_credentials=False)["artifacts"]
        self.assertEqual([(item["kind"], item["path"]) for item in recent], [("model", str(model_path))])
        assets = self.run_cli("asset", "list", local["task_id"], with_credentials=False)["assets"]
        self.assertEqual([item["remote_index"] for item in assets], [0, 1])

    def test_truncated_or_non_glb_download_is_a_download_failure_not_a_model(self):
        for marker, code in (("[截断模型]", "incomplete_model_file"), ("[非模型文件]", "invalid_model_asset")):
            with self.subTest(marker=marker):
                output_dir = self.temp_path / code
                submitted = self.run_cli(
                    "model", "generate", "--prompt", f"一只陶瓷茶壶 {marker}", "--detach",
                    "--output-dir", str(output_dir),
                )
                self.run_cli("task", "status", submitted["local_id"], "--refresh")
                failed = self.run_cli(
                    "task", "status", submitted["local_id"], "--refresh", expected_returncode=4,
                )["task"]
                self.assertEqual(failed["status"], "succeeded")
                self.assertFalse(failed["ok"])
                self.assertEqual(failed["download_status"], "failed")
                self.assertEqual((failed["error"]["code"], failed["error"]["phase"]), (code, "download"))
                self.assertEqual((failed["files"], failed["models"]), ([], []))
                self.assertEqual(list(output_dir.glob("*")), [])
                self.assertEqual(failed["next_action_args"], ["wait", submitted["local_id"]])
        self.assertEqual(self.submissions(), 2)


if __name__ == "__main__":
    unittest.main()
