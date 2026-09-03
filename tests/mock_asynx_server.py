#!/usr/bin/env python3
"""用于 asx skill 端到端测试的本地 Mock Asynx 服务。"""

from __future__ import annotations

import argparse
import base64
import json
import socket
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

API_KEY = "asx-mock-test"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _model(name: str, *, mask: bool) -> dict[str, Any]:
    return {
        "name": name,
        "modality": "image",
        "task_types": ["image.generate", "image.edit"],
        "capabilities": {
            "image_sizes": ["1K", "2K", "4K"],
            "aspect_ratios": ["1:1", "16:9", "9:16"],
            "output_formats": ["png", "jpeg", "webp"],
            "max_images": 4,
            "supports_reference_images": True,
            "max_reference_images": 8,
            "supports_mask": mask,
        },
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

    def submit(self, body: dict[str, Any], idempotency_key: str) -> tuple[dict[str, Any], bool, bool]:
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
        response["result_quality"] = "expected"
        response["result"] = {
            "assets": [
                {
                    "index": 0,
                    "id": f"asset_{task['id']}",
                    "kind": "image",
                    "content_type": "image/png",
                    "size_bytes": len(PNG),
                    "width": 1,
                    "height": 1,
                    "download_url": f"{base_url}/v1/tasks/{task['id']}/assets/0",
                    "download_url_expires_at": None,
                    "retention_expires_at": task["deadline_at"],
                }
            ]
        }
    elif status == "failed":
        response["error"] = {"code": "mock_failure", "message": "Mock 任务按测试要求失败"}
    elif status == "canceled":
        response["error"] = {"code": "task_canceled", "message": "Mock 任务已取消"}
    return response


class MockHandler(BaseHTTPRequestHandler):
    server: "MockServer"

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
                    }
                )
            return
        if not self._authorized():
            return
        if path == "/v1/tasks/models":
            self._ok({"items": [_model("gpt-image-2", mask=True), _model("gemini-3.1-flash-image", mask=False)]})
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
            if match[1] == "asset":
                if task["status"] != "succeeded":
                    self._error(409, "task_not_succeeded", "Task 尚未成功")
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(PNG)))
                self.end_headers()
                self.wfile.write(PNG)
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
            task, replayed, drop = self.server.state.submit(body, key)
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
