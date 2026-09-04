from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from urllib.request import urlopen

from PIL import Image  # type: ignore[import-not-found]

ROOT = Path(__file__).parents[1]
CLI = ROOT / "skills" / "asx" / "scripts" / "asynx.py"
SERVER = ROOT / "tests" / "mock_asynx_server.py"
VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class CLIE2ETestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = subprocess.Popen(
            [sys.executable, str(SERVER), "--port", "0"],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self.server.stdout is not None
        self.base_url = self.server.stdout.readline().strip()
        if not self.base_url:
            stderr = self.server.stderr.read() if self.server.stderr else ""
            self.fail(f"Mock Asynx 服务启动失败：{stderr}")
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temporary.name)
        self.environment = os.environ.copy()
        self.environment.update(
            {
                "ASYNX_API_KEY": "asx-mock-test",
                "ASYNX_BASE_URL": self.base_url,
                "ASYNX_STATE_PATH": str(self.temp_path / "state.db"),
                "ASYNX_CACHE_PATH": str(self.temp_path / "models.json"),
            }
        )

    def tearDown(self) -> None:
        self.server.terminate()
        try:
            self.server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.server.kill()
            self.server.wait(timeout=3)
        if self.server.stdout is not None:
            self.server.stdout.close()
        if self.server.stderr is not None:
            self.server.stderr.close()
        self.temporary.cleanup()

    def run_cli(self, *arguments: str, with_credentials: bool = True) -> dict[str, Any]:
        environment = self.environment.copy()
        if not with_credentials:
            environment.pop("ASYNX_API_KEY", None)
            environment.pop("ASYNX_BASE_URL", None)
        result = subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(result.returncode, 0, msg=f"stderr:\n{result.stderr}\nstdout:\n{result.stdout}")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            self.fail(f"CLI stdout 不是单个 JSON 对象：{result.stdout!r}")
        self.assertIsInstance(payload, dict)
        return cast(dict[str, Any], payload)

    def poll_until_finished(self, batch_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for _attempt in range(10):
            result = self.run_cli("batch", "poll", batch_id, "--limit", "2")
            if result["batch"]["status"] in {"completed", "canceled"}:
                return result
        self.fail(f"批次未在预期轮询次数内完成：{result}")

    def test_keep_reference_original_reaches_submission(self) -> None:
        source = self.temp_path / "source.png"
        source.write_bytes(VALID_PNG)

        submitted = self.run_cli(
            "generate",
            "--prompt",
            "保留参考图编码",
            "--reference",
            str(source),
            "--keep-reference-original",
            "--detach",
        )
        history = self.run_cli("history", "--limit", "1")
        task = history["tasks"][0]

        self.assertEqual(task["id"], submitted["task_id"])
        reference = task["input"]["reference_images"][0]
        self.assertTrue(reference.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(reference.split(",", 1)[1]), VALID_PNG)

    def test_local_task_commands_resume_detached_submission(self) -> None:
        submitted = self.run_cli("generate", "--prompt", "本地账本任务", "--detach")
        local_id = submitted["local_id"]
        task_id = submitted["task_id"]

        snapshot = self.run_cli("task", "status", local_id, with_credentials=False)
        self.assertEqual(snapshot["task"]["task_id"], task_id)
        self.assertEqual(snapshot["task"]["status"], "queued")

        polled = self.run_cli("task", "poll", local_id)
        self.assertEqual(polled["tasks"][0]["status"], "running")
        recovered = self.run_cli("task", "recover")
        self.assertEqual(recovered["tasks"][0]["status"], "succeeded")

        assets = self.run_cli("asset", "list", task_id, with_credentials=False)
        self.assertEqual(len(assets["assets"]), 1)
        self.assertFalse(assets["assets"][0]["state"] in {"missing", "deleted"})

        listed = self.run_cli("task", "list", "--status", "succeeded", with_credentials=False)
        self.assertTrue(any(item["local_id"] == local_id for item in listed["tasks"]))

    def test_wait_reuses_deterministic_asset_path(self) -> None:
        generated = self.run_cli("generate", "--prompt", "重复等待测试")
        first = generated["files"]
        second = self.run_cli("wait", generated["task_id"])
        self.assertEqual(second["files"], first)
        self.assertEqual(len(list(Path(first[0]).parent.glob("*.png"))), 1)

    def test_large_reference_warning_reaches_cli_output(self) -> None:
        source = self.temp_path / "noisy.png"
        Image.effect_noise((1600, 1600), 100).convert("RGB").save(source, format="PNG")

        submitted = self.run_cli(
            "generate",
            "--prompt",
            "高纹理参考图",
            "--reference",
            str(source),
            "--detach",
        )

        warning = submitted["warnings"][0]
        self.assertEqual(warning["code"], "large_reference_payload")
        self.assertGreaterEqual(warning["request_body_bytes"], 1024 * 1024)
        self.assertEqual(warning["local_image_count"], 1)

    def test_local_artifact_index_supports_recent_query_and_edit_from_task(self) -> None:
        generated = self.run_cli("generate", "--prompt", "可编辑的机械键盘")
        task_id = generated["task_id"]

        recent = self.run_cli(
            "recent",
            "--query",
            "机械键盘",
            "--limit",
            "5",
            with_credentials=False,
        )
        self.assertEqual(len(recent["artifacts"]), 1)
        self.assertEqual(recent["artifacts"][0]["task_id"], task_id)
        self.assertFalse(recent["artifacts"][0]["missing"])

        edited = self.run_cli(
            "edit",
            "--prompt",
            "改成深蓝色",
            "--from-task",
            task_id,
        )
        self.assertEqual(edited["status"], "succeeded")
        self.assertEqual(len(edited["files"]), 1)

    def test_real_cli_processes_resume_append_edit_cancel_and_history(self) -> None:
        source = self.temp_path / "source.png"
        source.write_bytes(VALID_PNG)

        fast = self.run_cli("generate", "--prompt", "快速路径", "--detach")
        self.assertEqual(fast["model"], "gpt-image-2")
        self.assertIn("prepare_seconds", fast["timings"])
        self.assertIn("submit_seconds", fast["timings"])

        recovered_reference = self.run_cli(
            "generate",
            "--prompt",
            "参考该图片的风格生成新图",
            "--reference",
            str(source),
            "--detach",
        )
        self.assertEqual(recovered_reference["status"], "queued")

        output_dir = self.temp_path / "generated"
        created = self.run_cli(
            "batch",
            "create",
            "--prompt",
            "[断线测试] 城市夜景",
            "--model",
            "gpt-image-2",
            "--total",
            "3",
            "--limit",
            "1",
            "--output-dir",
            str(output_dir),
        )
        batch_id = created["batch"]["id"]
        self.assertEqual(created["batch"]["submitted_now"], 1)

        added = self.run_cli(
            "batch",
            "add",
            batch_id,
            "--total",
            "2",
            "--prompt",
            "雨夜城市",
            "--limit",
            "1",
        )
        self.assertEqual(added["batch"]["total"], 5)

        local_status = self.run_cli("batch", "status", batch_id, "--items", with_credentials=False)
        self.assertEqual(local_status["batch"]["total"], 5)

        finished = self.poll_until_finished(batch_id)
        self.assertEqual(finished["batch"]["status"], "completed")
        self.assertEqual(len(list(output_dir.glob("*.png"))), 5)

        edit_output = self.temp_path / "edited"
        edit = self.run_cli(
            "batch",
            "create",
            "--operation",
            "edit",
            "--prompt",
            "替换为白色背景",
            "--image",
            str(source),
            "--model",
            "gpt-image-2",
            "--total",
            "1",
            "--output-dir",
            str(edit_output),
        )
        edit_id = edit["batch"]["id"]
        self.poll_until_finished(edit_id)
        self.assertEqual(len(list(edit_output.glob("*.png"))), 1)

        canceled = self.run_cli(
            "batch",
            "create",
            "--prompt",
            "需要取消的批次",
            "--total",
            "2",
            "--limit",
            "1",
        )
        cancel_id = canceled["batch"]["id"]
        canceled = self.run_cli("batch", "cancel", cancel_id)
        self.assertEqual(canceled["batch"]["status"], "canceled")

        batches = self.run_cli("batch", "list", with_credentials=False)
        self.assertEqual(len(batches["batches"]), 3)
        history = self.run_cli("history", "--limit", "20")
        self.assertEqual(len(history["tasks"]), 9)
        recovered = next(
            task
            for task in history["tasks"]
            if task["id"] == recovered_reference["task_id"]
        )
        self.assertEqual(recovered["input"]["prompt"], "参考该图片的风格生成新图")
        self.assertEqual(len(recovered["input"]["reference_images"]), 1)
        self.assertTrue(
            recovered["input"]["reference_images"][0].startswith("data:image/webp;base64,")
        )

        with urlopen(f"{self.base_url}/__mock__/stats", timeout=5) as response:
            stats = json.load(response)["data"]
        self.assertEqual(stats["tasks"], 9)
        self.assertGreaterEqual(stats["idempotent_replays"], 1)
        self.assertEqual(stats["model_requests"], 0)


if __name__ == "__main__":
    unittest.main()
