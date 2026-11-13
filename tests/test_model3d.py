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
from asxlib import cli, models3d, state, uploads
from asxlib.errors import AsxError
from asxlib.media_results import image_mime
from mock_asynx_server import FBX, GLB, MODEL_3D, MODEL_ZIP, MODELS_3D
from test_audit_regressions import FakeClient

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
        self.assertEqual(image_mime(uploads.read(local["image"])), "image/webp")
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


class ModelFollowUpBodyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        env = patch.dict(os.environ, {
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CONFIG_PATH": str(self.root / "config.json"),
            "ASYNX_CACHE_PATH": str(self.root / "models.json"),
            "ASYNX_UPDATE_CHECK": "off",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = FakeClient()
        self.client.catalog = MODELS_3D

    def finished(self, name, task_type):
        """A succeeded task in the local ledger: ``local_<name>`` here, ``task_<name>`` on the service."""
        with state.connect_db() as connection:
            state.create_task_intent(
                connection, operation=task_type, model="tripo-h3.1", idempotency_key=f"asx-{name}",
                request={"task_type": task_type, "model": "tripo-h3.1", "input": {}},
                output_dir=str(self.root), local_id=f"local_{name}", status="succeeded",
            )
            state.bind_remote_task(
                connection, f"local_{name}", f"task_{name}", status="succeeded",
                remote={"id": f"task_{name}", "status": "succeeded"},
            )

    def sent(self, *flags):
        args = cli.parser().parse_args(["model", *flags, "--detach"])
        with patch.object(cli, "_client", return_value=self.client):
            cli.execute(args)
        return self.client.submitted[-1][0]

    def test_follow_up_bodies_name_the_server_task_and_carry_only_contract_fields(self):
        # The service knows no local IDs and refuses unknown fields: either one loses the request.
        self.finished("generate", "model.generate")
        self.finished("rig", "model.rig")
        generated, rigged = {"task_id": "task_generate"}, {"task_id": "task_rig"}
        self.assertEqual(self.sent("rig-check", "--from-task", "local_generate"), {
            "task_type": "model.rig_check", "model": "tripo-rig", "input": {"source": generated},
        })
        self.assertEqual(
            self.sent("rig", "--from-task", "local_generate", "--rig-type", "biped", "--skeleton", "mixamo"),
            {"task_type": "model.rig", "model": "tripo-rig", "input": {
                "source": generated, "rig": {"type": "biped", "skeleton_profile": "mixamo"},
                "output": {"formats": ["glb"]},
            }},
        )
        # Unset rig options are left to the service instead of being sent as a guess.
        self.assertEqual(
            self.sent("rig", "--from-task", "local_generate", "--format", "fbx")["input"],
            {"source": generated, "output": {"formats": ["fbx"]}},
        )
        self.assertEqual(self.sent("animate", "--from-task", "local_rig", "--animation", "preset:walk"), {
            "task_type": "model.retarget", "model": "tripo-animate", "input": {
                "source": rigged, "animations": ["preset:walk"], "output": {"formats": ["glb"]},
            },
        })
        self.assertEqual(self.sent("convert", "--from-task", "local_rig", "--format", "fbx"), {
            "task_type": "model.convert", "model": "tripo-convert", "input": {
                "source": rigged, "output": {"formats": ["fbx"]},
            },
        })
        # A task this machine has no record of is named as given; the service decides.
        self.assertEqual(
            self.sent("convert", "--from-task", "task_elsewhere", "--format", "usdz")["input"]["source"],
            {"task_id": "task_elsewhere"},
        )


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

    def finished_model(self, prompt):
        """A generated model this machine knows as succeeded, to serve as a follow-up's source."""
        submitted = self.run_cli(
            "model", "generate", "--prompt", prompt, "--detach", "--output-dir", str(self.temp_path / "source"),
        )
        self.run_cli("task", "status", submitted["local_id"], "--refresh")
        self.run_cli("task", "status", submitted["local_id"], "--refresh")
        return submitted["local_id"]

    def test_unriggable_answer_is_a_successful_result_without_files(self):
        # "Not riggable" is the answer that was asked for; reporting it as a failed or
        # empty task would make an agent retry a check that already ran.
        source = self.finished_model("一只陶瓷茶壶 [不可绑定]")
        submitted = self.run_cli("model", "rig-check", "--from-task", source, "--detach")
        self.run_cli("task", "status", submitted["local_id"], "--refresh")
        answer = {"riggable": False, "recommended_rig_type": None}
        waited = self.run_cli("wait", submitted["local_id"])
        refreshed = self.run_cli("task", "status", submitted["task_id"], "--refresh")["task"]
        polled = self.run_cli("task", "poll", submitted["local_id"])["tasks"][0]
        for payload in (waited, refreshed, polled):
            self.assertTrue(payload["ok"])
            self.assertEqual((payload["status"], payload["rig_check"], payload["files"]), ("succeeded", answer, []))
            self.assertNotIn("error", payload)
            self.assertNotIn("next_action_args", payload)
        saved = self.run_cli("task", "status", submitted["local_id"], with_credentials=False)["task"]
        self.assertEqual(saved["rig_check"], answer)
        self.assertEqual(self.run_cli("history", "--limit", "1")["tasks"][0]["result"]["data"], answer)
        recent = self.run_cli("recent", "--query", submitted["task_id"], with_credentials=False)
        self.assertEqual(recent["artifacts"], [])
        self.assertEqual(self.submissions(), 2)

    def test_truncated_fbx_or_archive_conversion_is_a_download_failure_not_a_model(self):
        def converted(source, output_format, expected_returncode=0):
            output_dir = self.temp_path / f"{source}-{output_format}"
            submitted = self.run_cli(
                "model", "convert", "--from-task", source, "--format", output_format, "--detach",
                "--output-dir", str(output_dir),
            )
            self.run_cli("task", "status", submitted["local_id"], "--refresh")
            task = self.run_cli(
                "task", "status", submitted["local_id"], "--refresh", expected_returncode=expected_returncode,
            )["task"]
            return task, submitted["local_id"], output_dir

        whole = self.finished_model("一只陶瓷茶壶")
        cut = self.finished_model("一只陶瓷茶壶 [截断后续]")
        for output_format, content, suffix in (("fbx", FBX, ".fbx"), ("obj", MODEL_ZIP, ".zip")):
            with self.subTest(format=output_format):
                # The same conversion is delivered when it arrives whole, kept as the
                # service sent it, so the refusal below is about the missing bytes.
                delivered, _local_id, _output_dir = converted(whole, output_format)
                model_path, preview_path = (Path(value) for value in delivered["files"])
                self.assertEqual((model_path.suffix, model_path.read_bytes()), (suffix, content))
                self.assertEqual([item["path"] for item in delivered["models"]], [str(model_path)])
                self.assertEqual([item["path"] for item in delivered["images"]], [str(preview_path)])

                failed, local_id, output_dir = converted(cut, output_format, expected_returncode=4)
                self.assertEqual(failed["status"], "succeeded")
                self.assertFalse(failed["ok"])
                self.assertEqual(failed["download_status"], "failed")
                self.assertEqual(
                    (failed["error"]["code"], failed["error"]["phase"]), ("incomplete_model_file", "download"),
                )
                self.assertEqual((failed["files"], failed["models"]), ([], []))
                self.assertEqual(list(output_dir.glob("*")), [])
                self.assertEqual(failed["next_action_args"], ["wait", local_id])
        # An FBX or an archive is listed as a model although its media type is application/*.
        recent = self.run_cli("recent", "--modality", "model", with_credentials=False)["artifacts"]
        self.assertEqual({item["kind"] for item in recent}, {"model"})
        self.assertEqual(sorted(Path(item["path"]).suffix for item in recent), [".fbx", ".glb", ".glb", ".zip"])
        self.assertEqual(self.submissions(), 6)


if __name__ == "__main__":
    unittest.main()
