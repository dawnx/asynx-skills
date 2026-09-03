from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "asx" / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

import install as installer  # noqa: E402
from asxlib import AsxError, AsynxClient, configure  # noqa: E402
from asxlib import batches, config, images, tasks  # noqa: E402
from asxlib.constants import DEFAULT_BASE_URL  # noqa: E402


def model(name: str, *, mask: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "modality": "image",
        "task_types": ["image.generate", "image.edit"],
        "capabilities": {
            "image_sizes": ["1K", "2K", "4K"],
            "aspect_ratios": ["1:1", "16:9"],
            "output_formats": ["png", "jpeg", "webp"],
            "max_images": 4,
            "supports_reference_images": True,
            "max_reference_images": 8,
            "supports_mask": mask,
        },
        "pricing": {"mode": "per_request", "amount": "0.01", "currency": "USD"},
    }


class FakeAsynxHandler(BaseHTTPRequestHandler):
    task_reads = 0
    submission: dict[str, Any] | None = None
    idempotency_key: str | None = None
    image = b"\x89PNG\r\n\x1a\nresult"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _json(self, status: int, data: dict[str, Any]) -> None:
        raw = json.dumps({"code": "ok", "message": None, "data": data, "request_id": "req_test"}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @classmethod
    def task(cls, status: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": "task_test",
            "task_type": "image.generate",
            "model": "gpt-image-2",
            "status": status,
            "deadline_at": "2099-01-01T00:00:00Z",
            "billing": {"currency": "USD", "status": "reserved", "estimated_amount": "0.01"},
        }
        if status == "succeeded":
            payload["result_quality"] = "expected"
            payload["billing"] = {"currency": "USD", "status": "captured", "captured_amount": "0.01"}
            payload["result"] = {
                "assets": [{"index": 0, "content_type": "image/png", "size_bytes": len(cls.image)}]
            }
        return payload

    def do_GET(self) -> None:
        if self.path == "/v1/tasks/models":
            self._json(200, {"items": [model("gpt-image-2", mask=True), model("gemini-3.1-flash-image")]})
            return
        if self.path == "/v1/tasks/task_test":
            type(self).task_reads += 1
            status = "running" if type(self).task_reads == 1 else "succeeded"
            self._json(200, self.task(status))
            return
        if self.path == "/v1/tasks/task_test/assets/0":
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(self.image)))
            self.end_headers()
            self.wfile.write(self.image)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if self.path != "/v1/tasks":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        type(self).submission = json.loads(self.rfile.read(length))
        type(self).idempotency_key = self.headers.get("Idempotency-Key")
        self._json(202, self.task("queued"))


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsynxHandler.task_reads = 0
        FakeAsynxHandler.submission = None
        FakeAsynxHandler.idempotency_key = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAsynxHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host = str(self.server.server_address[0])
        port = int(self.server.server_address[1])
        self.client = AsynxClient(f"http://{host}:{port}", "asx-test")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_async_submission_wait_and_download(self) -> None:
        body, selected, _request_id = images.build_task(
            self.client,
            task_type="image.generate",
            model_selector="gpt",
            prompt="A product photo",
            image_size="2k",
            aspect_ratio="16:9",
            quality="high",
            count=1,
            output_format="png",
            references=[],
        )
        self.assertEqual(selected, "gpt-image-2")
        task, request_id = self.client.submit(body, "asx-test-idempotency")
        task, request_id = tasks.wait_for_terminal(
            self.client, task, request_id, sleep=lambda _seconds: None
        )
        with tempfile.TemporaryDirectory() as directory:
            files = tasks.download_assets(self.client, task, directory)
            self.assertEqual(Path(files[0]).read_bytes(), FakeAsynxHandler.image)
        self.assertEqual(task["status"], "succeeded")
        self.assertEqual(FakeAsynxHandler.idempotency_key, "asx-test-idempotency")
        assert FakeAsynxHandler.submission is not None
        self.assertEqual(FakeAsynxHandler.submission["task_type"], "image.generate")
        self.assertEqual(FakeAsynxHandler.submission["input"]["image_size"], "2K")


class UnitTestCase(unittest.TestCase):
    def test_model_resolution_is_dynamic_and_detects_ambiguity(self) -> None:
        models = [model("gpt-image-2"), model("gemini-2.5-flash-image"), model("gemini-3.1-flash-image")]
        self.assertEqual(images.resolve_model(models, "gemini 3.1", "image.generate")["name"], "gemini-3.1-flash-image")
        with self.assertRaises(AsxError) as caught:
            images.resolve_model(models, "gemini", "image.generate")
        self.assertEqual(caught.exception.code, "ambiguous_model")

    def test_edit_requires_a_model_with_mask_support(self) -> None:
        class CatalogClient:
            def models(self) -> tuple[list[dict[str, Any]], str]:
                return [model("gemini-3.1-flash-image")], "req_catalog"

        with self.assertRaises(AsxError) as caught:
            images.build_task(
                CatalogClient(),
                task_type="image.edit",
                model_selector="gemini 3.1",
                prompt="Replace the background",
                image_size="1K",
                aspect_ratio="1:1",
                quality="standard",
                count=1,
                output_format="png",
                references=["data:image/png;base64,iVBORw0KGgpyZWY="],
                mask="data:image/png;base64,iVBORw0KGgptYXNr",
            )
        self.assertEqual(caught.exception.code, "unsupported_model_capability")

    def test_local_image_becomes_data_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.png"
            path.write_bytes(b"\x89PNG\r\n\x1a\nsource")
            source = images.image_source(str(path))
        self.assertTrue(source.startswith("data:image/png;base64,"))

    def test_configure_only_prompts_for_api_key(self) -> None:
        class TTY:
            @staticmethod
            def isatty() -> bool:
                return True

        with tempfile.TemporaryDirectory() as directory:
            config_file = Path(directory) / "config.json"
            with (
                patch.dict(os.environ, {"ASYNX_CONFIG_PATH": str(config_file)}, clear=False),
                patch("asxlib.config.sys.stdin", TTY()),
                patch("asxlib.config.getpass.getpass", return_value="asx-test-key") as prompt,
            ):
                result = configure()
            saved = json.loads(config_file.read_text(encoding="utf-8"))
        prompt.assert_called_once()
        self.assertEqual(saved["api_key"], "asx-test-key")
        self.assertEqual(saved["base_url"], DEFAULT_BASE_URL)
        self.assertTrue(result["configured"])

    def test_installer_detects_and_copies_to_codex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".codex").mkdir()
            self.assertEqual(installer._resolve_targets("auto", home), ["codex"])
            destination = installer._target_path("codex", home)
            installer._install_skill(destination)
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertTrue((destination / "scripts" / "asynx.py").is_file())
            installed = subprocess.run(
                [sys.executable, str(destination / "scripts" / "asynx.py"), "--version"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertEqual(installed.stdout.strip(), "0.1.0")

    def test_installer_can_install_both_agents_noninteractively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = installer.run(
                ["--target", "both", "--skip-config", "--no-verify"],
                home=home,
            )
            self.assertEqual(result, 0)
            self.assertTrue((home / ".agents" / "skills" / "asx" / "SKILL.md").is_file())
            self.assertTrue((home / ".claude" / "skills" / "asx" / "SKILL.md").is_file())

    def test_installer_uninstall_only_removes_installed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            destination = installer._target_path("codex", home)
            installer._install_skill(destination)
            self.assertTrue(installer._uninstall_skill(destination))
            self.assertFalse(destination.exists())
            self.assertFalse(installer._uninstall_skill(destination))

    def test_api_key_rejects_provider_keys(self) -> None:
        with self.assertRaises(AsxError) as caught:
            config.validate_api_key("sk-provider-key")
        self.assertEqual(caught.exception.code, "invalid_api_key")

    def test_batch_create_poll_and_append_are_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            with patch.dict(os.environ, {"ASYNX_STATE_PATH": str(state)}, clear=False):
                server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAsynxHandler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    host = str(server.server_address[0])
                    port = int(server.server_address[1])
                    client = AsynxClient(f"http://{host}:{port}", "asx-test")
                    created = batches.create_batch(
                        client,
                        operation="generate",
                        prompt="A city",
                        model_selector="gpt-image-2",
                        image_size="1K",
                        aspect_ratio="1:1",
                        quality="standard",
                        count=1,
                        output_format="png",
                        references=[],
                        mask=None,
                        total=2,
                        name="测试批次",
                        output_dir=str(Path(directory) / "outputs"),
                    )
                    self.assertEqual(created["total"], 2)
                    added = batches.add_to_batch(
                        client,
                        created["id"],
                        total=1,
                        prompt="A rainy city",
                        references=None,
                        mask=None,
                        count=None,
                        image_size=None,
                        aspect_ratio=None,
                        quality=None,
                        output_format=None,
                    )
                    self.assertEqual(added["total"], 3)
                    progressed = batches.poll_batch(client, created["id"], submissions_limit=2)
                    self.assertEqual(progressed["batch"]["total"], 3)
                    status = batches.batch_status(created["id"], include_items=True)
                    self.assertEqual(len(status["batch"]["items"]), 3)
                    self.assertIn(status["batch"]["items"][-1]["status"], {"pending", "queued", "running", "succeeded"})
                    final = batches.poll_batch(client, created["id"], submissions_limit=2)
                    self.assertEqual(final["batch"]["status"], "completed")
                    self.assertEqual(len(list((Path(directory) / "outputs").glob("*.png"))), 3)
                    with self.assertRaises(AsxError) as caught:
                        batches.add_to_batch(
                            client,
                            created["id"],
                            total=1,
                            prompt="Too late",
                            references=None,
                            mask=None,
                            count=None,
                            image_size=None,
                            aspect_ratio=None,
                            quality=None,
                            output_format=None,
                        )
                    self.assertEqual(caught.exception.code, "batch_not_appendable")
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    def test_omitted_batch_id_rejects_multiple_active_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            with patch.dict(os.environ, {"ASYNX_STATE_PATH": str(state)}, clear=False):
                connection = batches.connect_db()
                now = batches.utc_now()
                try:
                    for batch_id in ("batch_one", "batch_two"):
                        connection.execute(
                            "INSERT INTO batches "
                            "(id, operation, name, status, model, output_dir, template_json, created_at, updated_at) "
                            "VALUES (?, 'generate', ?, 'active', 'gpt-image-2', ?, '{}', ?, ?)",
                            (batch_id, batch_id, directory, now, now),
                        )
                    connection.commit()
                    with self.assertRaises(AsxError) as caught:
                        batches.get_batch(connection, None)
                    self.assertEqual(caught.exception.code, "ambiguous_batch")
                finally:
                    connection.close()


if __name__ == "__main__":
    unittest.main()
