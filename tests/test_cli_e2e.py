from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, cast
from urllib.request import urlopen

ROOT = Path(__file__).parents[1]
CLI = ROOT / "skills" / "asx" / "scripts" / "asynx.py"
SERVER = ROOT / "tests" / "mock_asynx_server.py"


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

    def test_real_cli_processes_resume_append_edit_cancel_and_history(self) -> None:
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

        source = self.temp_path / "source.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nsource")
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
        self.assertEqual(len(history["tasks"]), 7)

        with urlopen(f"{self.base_url}/__mock__/stats", timeout=5) as response:
            stats = json.load(response)["data"]
        self.assertEqual(stats["tasks"], 7)
        self.assertGreaterEqual(stats["idempotent_replays"], 1)


if __name__ == "__main__":
    unittest.main()
