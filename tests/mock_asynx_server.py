"""用于 asynx-image skill 端到端测试的本地 Mock Asynx 服务。"""

from __future__ import annotations

import argparse
import base64
import io
import json
import socket
import threading
import zipfile
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

API_KEY = "asx-mock-test"
SUNBURST_QUALITIES = ("low", "medium", "high", "xhigh", "max", "auto")
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
# A small MP4 container fixture for transport tests, not a playable movie.
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42" + b"\x00\x00\x00\x0cmdatTEST"
# A minimal binary glTF container for transport tests, not a renderable model:
# magic, version 2, total length (little-endian uint32), then one JSON chunk.
_GLB_JSON = b'{"asset":{"version":"2.0"}} '
GLB = (
    b"glTF" + (2).to_bytes(4, "little") + (20 + len(_GLB_JSON)).to_bytes(4, "little")
    + len(_GLB_JSON).to_bytes(4, "little") + b"JSON" + _GLB_JSON
)
# A binary FBX header for transport tests, not a loadable scene.
FBX = b"Kaydara FBX Binary  \x00\x1a\x00" + (7400).to_bytes(4, "little") + bytes(13)


def _archive() -> bytes:
    # A conversion delivered as an archive holding the model. ZipInfo's fixed
    # entry date keeps the bytes identical in the server and the test process.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(zipfile.ZipInfo("model.obj"), "v 0 0 0\n")
    return buffer.getvalue()


MODEL_ZIP = _archive()


def _model3d(name: str, *task_types: str) -> dict[str, Any]:
    return {
        "name": name,
        "modality": "model",
        "task_types": list(task_types),
        "capabilities": {"task_types": list(task_types)},
        "pricing": {"mode": "per_request", "amount": "0.20000000", "currency": "USD"},
    }


MODEL_3D = _model3d("tripo-h3.1", "model.generate")
# Operators publish the follow-up operations as separate catalog entries.
MODELS_3D = [
    MODEL_3D,
    _model3d("tripo-rig", "model.rig_check", "model.rig"),
    _model3d("tripo-animate", "model.retarget"),
    _model3d("tripo-convert", "model.convert"),
]


def _model_file(task: dict[str, Any]) -> tuple[bytes, str]:
    """The file a 3D task delivers: GLB unless its request names another format."""
    output = task["input"].get("output")
    requested = output["formats"][0] if isinstance(output, dict) and output.get("formats") else "glb"
    if requested == "glb":
        return GLB, "model/gltf-binary"
    if requested == "fbx":
        return FBX, "application/vnd.autodesk.fbx"
    # Any other conversion arrives as an archive holding the model.
    return MODEL_ZIP, "application/zip"


def _model(name: str, *, mask: bool) -> dict[str, Any]:
    return {
        "name": name,
        "modality": "image",
        "task_types": ["image.generate", "image.edit"],
        "capabilities": {"task_types": ["image.generate", "image.edit"]},
        "pricing": {"mode": "per_request", "amount": "0.01000000", "currency": "USD"},
        "official_discount_percent": None,
    }


class MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.tasks: dict[str, dict[str, Any]] = {}
        self.idempotency: dict[str, str] = {}
        self.dropped: set[str] = set()
        self.submissions = 0
        self.replays = 0
        self.model_requests = 0

    def markers(self, body: dict[str, Any]) -> str | None:
        """Test markers for a new task: its prompt, or for a 3D follow-up those of its source.

        ``None`` means the request names a source task that does not exist.
        """
        task_input = body.get("input")
        source = task_input.get("source") if isinstance(task_input, dict) else None
        if not isinstance(source, dict):
            prompt = task_input.get("prompt") if isinstance(task_input, dict) else None
            return prompt if isinstance(prompt, str) else ""
        with self.lock:
            task = self.tasks.get(str(source.get("task_id")))
        return None if task is None else str(task["markers"])

    def submit(
        self, body: dict[str, Any], idempotency_key: str, markers: str
    ) -> tuple[dict[str, Any], bool, bool]:
        with self.lock:
            existing_id = self.idempotency.get(idempotency_key)
            if existing_id:
                self.replays += 1
                return self.tasks[existing_id], True, False
            self.submissions += 1
            task_id = f"task_mock_{self.submissions:04d}"
            task = {
                "id": task_id,
                "task_type": body.get("task_type"),
                "model": body.get("model"),
                "input": body.get("input"),
                "metadata": body.get("metadata", {}),
                "markers": markers,
                "status": "queued",
                "polls": 0,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(
                    timespec="seconds"
                ),
            }
            self.tasks[task_id] = task
            self.idempotency[idempotency_key] = task_id
            prompt = body.get("input", {}).get("prompt") if isinstance(body.get("input"), dict) else None
            drop = isinstance(prompt, str) and "[断线测试]" in prompt and idempotency_key not in self.dropped
            if drop:
                self.dropped.add(idempotency_key)
            return task, False, drop

    def read(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return None
            if task["status"] not in {"succeeded", "failed", "timeout", "canceled"}:
                task["polls"] += 1
                prompt = task.get("input", {}).get("prompt") if isinstance(task.get("input"), dict) else ""
                if task["polls"] == 1:
                    task["status"] = "running"
                elif (
                    task.get("model") == "gpt-image-2.5-sunburst"
                    and isinstance(task.get("input"), dict)
                    and "quality" in task["input"]
                    and task["input"]["quality"] not in SUNBURST_QUALITIES
                ):
                    task["status"] = "failed"
                    task["error"] = {
                        "code": "provider_request_rejected",
                        "message": (
                            f"Invalid value for quality: {task['input'].get('quality', 'standard')!r}. "
                            f"Supported values are: {', '.join(SUNBURST_QUALITIES)}."
                        ),
                    }
                elif isinstance(prompt, str) and "[失败测试]" in prompt:
                    task["status"] = "failed"
                else:
                    task["status"] = "succeeded"
            return task

    def cancel(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            task = self.tasks.get(task_id)
            if task is None:
                return None
            if task["status"] not in {"succeeded", "failed", "timeout", "canceled"}:
                task["status"] = "canceled"
            return task

    def list(self, status: str | None, model: str | None, limit: int) -> list[dict[str, Any]]:
        with self.lock:
            tasks = list(reversed(self.tasks.values()))
            if status:
                tasks = [task for task in tasks if task["status"] == status]
            if model:
                tasks = [task for task in tasks if task["model"] == model]
            return tasks[:limit]


def _public_task(task: dict[str, Any], base_url: str) -> dict[str, Any]:
    status = task["status"]
    response: dict[str, Any] = {
        "id": task["id"],
        "idempotency_key": None,
        "object": "task",
        "task_type": task["task_type"],
        "model": task["model"],
        "status": status,
        "created_at": task["created_at"],
        "deadline_at": task["deadline_at"],
        "started_at": task["created_at"] if status != "queued" else None,
        "finished_at": task["created_at"] if status in {"succeeded", "failed", "canceled"} else None,
        "attempt_count": 1 if status != "queued" else 0,
        "input": task["input"],
        "metadata": task["metadata"],
        "result": None,
        "error": None,
        "billing": {
            "currency": "USD",
            "status": "captured" if status == "succeeded" else "released" if status in {"failed", "canceled"} else "reserved",
            "estimated_amount": "0.01000000",
            "captured_amount": "0.01000000" if status == "succeeded" else "0.00000000",
            "released_amount": "0.01000000" if status in {"failed", "canceled"} else "0.00000000",
        },
    }
    if status == "succeeded":
        video = task["task_type"] == "video.generate"
        response["result_quality"] = "expected"
        if task["task_type"] == "model.rig_check":
            # A check delivers its answer as data and no files; "not riggable" still succeeds.
            riggable = "[不可绑定]" not in task["markers"]
            response["result"] = {
                "assets": [],
                "data": {"riggable": riggable, "recommended_rig_type": "biped" if riggable else None},
            }
            return response
        if task["task_type"].startswith("model."):
            links = [f"{base_url}/v1/tasks/{task['id']}/assets/{index}" for index in (0, 1)]
            content, content_type = _model_file(task)
            response["result"] = {
                "assets": [{
                    "index": 0, "id": f"asset_{task['id']}", "kind": "model", "role": "output_model",
                    "content_type": content_type, "size_bytes": len(content),
                    "width": None, "height": None, "download_url": links[0],
                }],
                # Rigged and animated models come without a preview image.
                "auxiliary_assets": [{
                    "index": 1, "id": f"asset_{task['id']}_preview", "kind": "image", "role": "output_preview",
                    "content_type": "image/png", "width": 1, "height": 1, "download_url": links[1],
                }] if task["task_type"] in {"model.generate", "model.convert"} else [],
                "image_count": None,
            }
            return response
        response["result"] = {
            "assets": [
                {
                    "index": 0,
                    "id": f"asset_{task['id']}",
                    "kind": "video" if video else "image",
                    "content_type": "video/mp4" if video else "image/png",
                    "size_bytes": len(MP4) if video else len(PNG),
                    "width": 1,
                    "height": 1,
                    "download_url": f"{base_url}/v1/tasks/{task['id']}/assets/0",
                    "download_url_expires_at": None,
                    "retention_expires_at": task["deadline_at"],
                }
            ]
        }
    elif status == "failed":
        response["error"] = task.get("error") or {
            "code": "mock_failure", "message": "Mock 任务按测试要求失败"
        }
    elif status == "canceled":
        response["error"] = {"code": "task_canceled", "message": "Mock 任务已取消"}
    return response


class MockHandler(BaseHTTPRequestHandler):
    server: MockServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{int(port)}"

    def _authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {API_KEY}":
            return True
        self._error(401, "invalid_api_key", "Mock API Key 无效")
        return False

    def _send(self, status: int, body: dict[str, Any], *, headers: dict[str, str] | None = None) -> None:
        raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Request-ID", "req_mock")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _ok(self, data: dict[str, Any], status: int = 200, **headers: str) -> None:
        self._send(
            status,
            {"code": "ok", "message": None, "data": data, "request_id": "req_mock"},
            headers=headers,
        )

    def _error(self, status: int, code: str, message: str) -> None:
        self._send(
            status,
            {"code": code, "message": message, "data": {"task_id": None}, "request_id": "req_mock"},
        )

    def _json_body(self) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._error(400, "invalid_json", "请求体不是有效 JSON")
            return None
        if not isinstance(value, dict):
            self._error(422, "validation_error", "请求体必须是 JSON Object")
            return None
        return value

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/health":
            self._ok({"status": "ok"})
            return
        if path == "/__mock__/stats":
            state = self.server.state
            with state.lock:
                self._ok(
                    {
                        "tasks": len(state.tasks),
                        "submissions": state.submissions,
                        "idempotent_replays": state.replays,
                        "model_requests": state.model_requests,
                    }
                )
            return
        if not self._authorized():
            return
        if path == "/v1/tasks/models":
            with self.server.state.lock:
                self.server.state.model_requests += 1
            self._ok({"items": [
                _model("gpt-image-2", mask=True), _model("gemini-3.1-flash-image", mask=False), *MODELS_3D,
            ]})
            return
        if path == "/v1/tasks":
            query = parse_qs(parsed.query)
            status = query.get("status", [None])[0]
            model = query.get("model", [None])[0]
            try:
                limit = int(query.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            items = [
                _public_task(task, self.base_url)
                for task in self.server.state.list(status, model, limit)
            ]
            self._ok({"items": items, "next_cursor": None})
            return
        match = re_task(path)
        if match:
            task = self.server.state.read(match[0])
            if task is None:
                self._error(404, "not_found", "Mock Task 不存在")
                return
            model3d = task["task_type"].startswith("model.")
            if (
                match[1] == "preview" and task["task_type"] not in {"model.generate", "model.convert"}
            ) or (match[1] == "asset" and task["task_type"] == "model.rig_check"):
                self._error(404, "not_found", "Mock 路由不存在")
                return
            if match[1] in {"asset", "preview"}:
                if task["status"] != "succeeded":
                    self._error(409, "task_not_succeeded", "Task 尚未成功")
                    return
                self.send_response(200)
                video = task["task_type"] == "video.generate"
                content = MP4 if video else PNG
                content_type = "video/mp4" if video else "image/png"
                if model3d and match[1] == "asset":
                    content, content_type = _model_file(task)
                    markers = task["markers"]
                    follow_up = isinstance(task["input"].get("source"), dict)
                    # Both bodies arrive whole, so only content checks can tell they are not a model.
                    if "[截断模型]" in markers or (follow_up and "[截断后续]" in markers):
                        content = content[:-4]
                    elif "[非模型文件]" in markers:
                        content = b"<html>Access denied</html>"
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
                return
            self._ok(_public_task(task, self.base_url))
            return
        self._error(404, "not_found", "Mock 路由不存在")

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if not self._authorized():
            return
        if path == "/v1/tasks":
            body = self._json_body()
            if body is None:
                return
            key = self.headers.get("Idempotency-Key")
            if not key:
                self._error(400, "invalid_idempotency_key", "缺少 Idempotency-Key")
                return
            markers = self.server.state.markers(body)
            if markers is None:
                self._error(404, "model_source_not_found", "Mock 来源 Task 不存在")
                return
            task, replayed, drop = self.server.state.submit(body, key, markers)
            if drop:
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.connection.close()
                return
            headers = {"Idempotent-Replayed": "true"} if replayed else {}
            self._ok(_public_task(task, self.base_url), 202, **headers)
            return
        match = re_task(path)
        if match and match[1] == "cancel":
            canceled_task = self.server.state.cancel(match[0])
            if canceled_task is None:
                self._error(404, "not_found", "Mock Task 不存在")
                return
            self._ok(_public_task(canceled_task, self.base_url))
            return
        self._error(404, "not_found", "Mock 路由不存在")


def re_task(path: str) -> tuple[str, str] | None:
    parts = path.strip("/").split("/")
    if len(parts) == 3 and parts[:2] == ["v1", "tasks"]:
        return parts[2], "task"
    if len(parts) == 4 and parts[:2] == ["v1", "tasks"] and parts[3] == "cancel":
        return parts[2], "cancel"
    if len(parts) == 5 and parts[:2] == ["v1", "tasks"] and parts[3:] == ["assets", "0"]:
        return parts[2], "asset"
    if len(parts) == 5 and parts[:2] == ["v1", "tasks"] and parts[3:] == ["assets", "1"]:
        return parts[2], "preview"
    return None


class MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, MockHandler)
        self.state = MockState()


def main() -> None:
    parser = argparse.ArgumentParser(description="启动本地 Mock Asynx 服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    server = MockServer((args.host, args.port))
    host, port = server.server_address[:2]
    host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
    print(f"http://{host_text}:{int(port)}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
