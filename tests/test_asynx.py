from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
SCRIPTS = ROOT / "skills" / "asx" / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS))

from asxlib import (
    AsxError,
    AsynxClient,
    batches,
    config,
    configure,
    images,
    reference_media,
    state,
    tasks,
)
from asxlib.constants import DEFAULT_BASE_URL

import install as installer

VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


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
    task_reads: ClassVar[dict[str, int]] = {}
    submission_count = 0
    submission: dict[str, Any] | None = None
    idempotency_key: str | None = None
    image = VALID_PNG

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
    def task(cls, status: str, task_id: str = "task_test") -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": task_id,
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
        if self.path.startswith("/v1/tasks/") and "/assets/" not in self.path:
            task_id = self.path.rsplit("/", 1)[-1]
            type(self).task_reads[task_id] = type(self).task_reads.get(task_id, 0) + 1
            status = "running" if type(self).task_reads[task_id] == 1 else "succeeded"
            self._json(200, self.task(status, task_id))
            return
        if self.path.startswith("/v1/tasks/") and self.path.endswith("/assets/0"):
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
        type(self).submission_count += 1
        task_id = "task_test" if type(self).submission_count == 1 else f"task_test_{type(self).submission_count}"
        self._json(202, self.task("queued", task_id))


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        FakeAsynxHandler.task_reads = {}
        FakeAsynxHandler.submission_count = 0
        FakeAsynxHandler.submission = None
        FakeAsynxHandler.idempotency_key = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAsynxHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.cache_directory = tempfile.TemporaryDirectory()
        self.cache_environment = patch.dict(
            os.environ,
            {"ASYNX_CACHE_PATH": str(Path(self.cache_directory.name) / "models.json")},
            clear=False,
        )
        self.cache_environment.start()
        host = str(self.server.server_address[0])
        port = int(self.server.server_address[1])
        self.client = AsynxClient(f"http://{host}:{port}", "asx-test")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.cache_environment.stop()
        self.cache_directory.cleanup()

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
    def test_recover_resubmits_persisted_intent_with_same_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            FakeAsynxHandler.task_reads = {}
            FakeAsynxHandler.submission_count = 0
            state_path = Path(directory) / "state.db"
            server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAsynxHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                host, port = server.server_address[:2]
                host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
                client = AsynxClient(f"http://{host_text}:{int(port)}", "asx-test")
                with patch.dict(
                    os.environ,
                    {
                        "ASYNX_STATE_PATH": str(state_path),
                        "ASYNX_CACHE_PATH": str(Path(directory) / "models.json"),
                    },
                    clear=False,
                ):
                    connection = state.connect_db()
                    try:
                        intent = state.create_task_intent(
                            connection,
                            operation="generate",
                            model="gpt-image-2",
                            idempotency_key="asx-recover-key",
                            request={"task_type": "image.generate", "input": {"prompt": "恢复"}},
                            output_dir=str(Path(directory) / "outputs"),
                            base_url=client.base_url,
                            status="submitting",
                        )
                        connection.commit()
                    finally:
                        connection.close()
                    result = tasks.recover_tasks(client)
                self.assertTrue(result["ok"])
                self.assertEqual(result["tasks"][0]["status"], "running")
                with patch.dict(
                    os.environ,
                    {"ASYNX_STATE_PATH": str(state_path)},
                    clear=False,
                ):
                    final = tasks.poll_local_tasks(client, str(intent["local_id"]))
                self.assertEqual(final["tasks"][0]["status"], "succeeded")
                self.assertEqual(FakeAsynxHandler.submission_count, 1)
                self.assertEqual(intent["idempotency_key"], "asx-recover-key")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_model_resolution_is_dynamic_and_detects_ambiguity(self) -> None:
        models = [model("gpt-image-2"), model("gemini-2.5-flash-image"), model("gemini-3.1-flash-image")]
        self.assertEqual(images.resolve_model(models, "gemini 3.1", "image.generate")["name"], "gemini-3.1-flash-image")
        with self.assertRaises(AsxError) as caught:
            images.resolve_model(models, "gemini", "image.generate")
        self.assertEqual(caught.exception.code, "ambiguous_model")

    def test_edit_requires_a_model_with_mask_support(self) -> None:
        class CatalogClient:
            base_url = "https://catalog.test"

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
            path.write_bytes(VALID_PNG)
            source = reference_media.prepare_image(str(path)).source
        self.assertTrue(source.startswith("data:image/webp;base64,"))

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
            installer._install_skill(destination, install_dependencies=False)
            self.assertTrue((destination / "SKILL.md").is_file())
            self.assertTrue((destination / "scripts" / "asynx.py").is_file())
            installed = subprocess.run(
                [sys.executable, str(destination / "scripts" / "asynx.py"), "--version"],
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            self.assertEqual(installed.stdout.strip(), "0.3.0")

    def test_installer_can_install_both_agents_noninteractively(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            result = installer.run(
                ["--target", "both", "--skip-config", "--no-verify"],
                home=home,
                install_dependencies=False,
            )
            self.assertEqual(result, 0)
            self.assertTrue((home / ".agents" / "skills" / "asx" / "SKILL.md").is_file())
            self.assertTrue((home / ".claude" / "skills" / "asx" / "SKILL.md").is_file())

    def test_installer_uninstall_only_removes_installed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            destination = installer._target_path("codex", home)
            installer._install_skill(destination, install_dependencies=False)
            self.assertTrue(installer._uninstall_skill(destination))
            self.assertFalse(destination.exists())
            self.assertFalse(installer._uninstall_skill(destination))

    def test_installer_places_pillow_in_private_vendor_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / ".agents" / "skills" / "asx"
            with patch("install.subprocess.run") as pip:
                pip.return_value.returncode = 0
                installer._install_skill(destination)

        command = pip.call_args.args[0]
        target_index = command.index("--target") + 1
        self.assertEqual(command[:3], [sys.executable, "-m", "pip"])
        self.assertEqual(command[target_index], str(destination / "scripts" / "vendor"))
        self.assertEqual(command[-1], installer.PILLOW_REQUIREMENT)
        self.assertFalse(pip.call_args.kwargs["check"])

    def test_api_key_rejects_provider_keys(self) -> None:
        with self.assertRaises(AsxError) as caught:
            config.validate_api_key("sk-provider-key")
        self.assertEqual(caught.exception.code, "invalid_api_key")

    def test_default_model_fast_path_skips_catalog(self) -> None:
        class NoCatalogClient:
            base_url = "https://fast-path.test"

            def models(self) -> tuple[list[dict[str, Any]], str | None]:
                raise AssertionError("default model fast path must not request the catalog")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"ASYNX_CACHE_PATH": str(Path(directory) / "models.json")},
            clear=False,
        ):
            body, selected, request_id = images.build_task(
                NoCatalogClient(),
                task_type="image.generate",
                model_selector=None,
                prompt="机械键盘",
                image_size="2k",
                aspect_ratio="1:1",
                quality="standard",
                count=1,
                output_format="png",
                references=[],
            )
        self.assertEqual(selected, "gpt-image-2")
        self.assertIsNone(request_id)
        self.assertEqual(body["input"]["image_size"], "2K")

    def test_fuzzy_model_uses_five_minute_cache(self) -> None:
        class CatalogClient:
            base_url = "https://cache.test"

            def __init__(self) -> None:
                self.calls = 0

            def models(self) -> tuple[list[dict[str, Any]], str]:
                self.calls += 1
                return [model("gemini-3.1-flash-image")], "req_catalog"

        class CachedClient:
            base_url = "https://cache.test"

            def models(self) -> tuple[list[dict[str, Any]], str | None]:
                raise AssertionError("fresh model cache must avoid a network request")

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"ASYNX_CACHE_PATH": str(Path(directory) / "models.json")},
            clear=False,
        ):
            first = CatalogClient()
            first_result = images.build_task(
                first,
                task_type="image.generate",
                model_selector="Gemini 3.1",
                prompt="产品图",
                image_size="1K",
                aspect_ratio="1:1",
                quality="standard",
                count=1,
                output_format="png",
                references=[],
            )
            cached_result = images.build_task(
                CachedClient(),
                task_type="image.generate",
                model_selector="Gemini 3.1",
                prompt="产品图",
                image_size="1K",
                aspect_ratio="1:1",
                quality="standard",
                count=1,
                output_format="png",
                references=[],
            )
        self.assertEqual(first.calls, 1)
        self.assertEqual(first_result[1], "gemini-3.1-flash-image")
        self.assertEqual(cached_result[1], "gemini-3.1-flash-image")

    def test_task_polling_starts_at_two_seconds_and_caps_at_four(self) -> None:
        class StatusClient:
            def __init__(self) -> None:
                self.statuses = iter(["running", "delayed", "running", "succeeded"])

            def task(self, task_id: str) -> tuple[dict[str, Any], str | None]:
                return {
                    "id": task_id,
                    "status": next(self.statuses),
                    "deadline_at": "2099-01-01T00:00:00Z",
                }, "req_poll"

        delays: list[float] = []
        with patch("asxlib.tasks.random.uniform", return_value=0.0):
            result, _request_id = tasks.wait_for_terminal(
                StatusClient(),
                {
                    "id": "task_poll",
                    "status": "queued",
                    "deadline_at": "2099-01-01T00:00:00Z",
                },
                "req_initial",
                sleep=delays.append,
            )
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(delays, [2.0, 3.0, 4.0, 4.0])

    def test_batch_create_poll_and_append_are_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.db"
            with patch.dict(
                os.environ,
                {
                    "ASYNX_STATE_PATH": str(state),
                    "ASYNX_CACHE_PATH": str(Path(directory) / "models.json"),
                },
                clear=False,
            ):
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
                    connection = batches.connect_db()
                    try:
                        item_bodies = [
                            json.loads(row["body_json"])
                            for row in connection.execute(
                                "SELECT body_json FROM batch_items WHERE batch_id = ?",
                                (created["id"],),
                            ).fetchall()
                        ]
                    finally:
                        connection.close()
                    self.assertTrue(all(body.get("_asx_inherit_template") for body in item_bodies))
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
                    if final["batch"]["status"] != "completed":
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
