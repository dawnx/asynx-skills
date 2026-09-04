from __future__ import annotations

import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "skills" / "asx" / "scripts"))

from asxlib import AsxError, AsynxClient


class RedirectHandler(BaseHTTPRequestHandler):
    mode: ClassVar[str] = "same_origin"
    target: ClassVar[str | None] = None
    requests: ClassVar[list[tuple[str, str | None]]] = []

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        type(self).requests.append((self.path, self.headers.get("Authorization")))
        if self.path == "/v1/tasks/models":
            if type(self).mode == "missing_location":
                self.send_response(302)
                self.end_headers()
                return
            location = (
                type(self).target
                if type(self).mode == "cross_origin"
                else "/redirected"
            )
            self.send_response(302)
            self.send_header("Location", location or "")
            self.end_headers()
            return
        if self.path == "/redirected":
            body = b'{"code":"ok","data":{"items":[]}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)


class ClientRedirectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        RedirectHandler.mode = "same_origin"
        RedirectHandler.target = None
        RedirectHandler.requests = []
        self.servers: list[ThreadingHTTPServer] = []
        self.threads: list[threading.Thread] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def _start_server(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        self.threads.append(thread)
        host, port = server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{int(port)}"

    def test_cross_origin_redirect_is_rejected_before_following(self) -> None:
        base_url = self._start_server()
        target_url = self._start_server() + "/leak"
        RedirectHandler.mode = "cross_origin"
        RedirectHandler.target = target_url

        client = AsynxClient(base_url, "asx-secret")

        with self.assertRaises(AsxError) as caught:
            client.models()

        self.assertEqual(caught.exception.code, "invalid_api_redirect")
        self.assertEqual(
            RedirectHandler.requests,
            [("/v1/tasks/models", "Bearer asx-secret")],
        )

    def test_same_origin_redirect_preserves_api_behavior(self) -> None:
        base_url = self._start_server()

        models, request_id = AsynxClient(base_url, "asx-secret").models()

        self.assertEqual(models, [])
        self.assertIsNone(request_id)
        self.assertEqual(
            RedirectHandler.requests,
            [
                ("/v1/tasks/models", "Bearer asx-secret"),
                ("/redirected", "Bearer asx-secret"),
            ],
        )

    def test_redirect_without_location_is_rejected(self) -> None:
        base_url = self._start_server()
        RedirectHandler.mode = "missing_location"

        with self.assertRaises(AsxError) as caught:
            AsynxClient(base_url, "asx-secret").models()

        self.assertEqual(caught.exception.code, "invalid_api_redirect")
        self.assertEqual(
            RedirectHandler.requests,
            [("/v1/tasks/models", "Bearer asx-secret")],
        )

if __name__ == "__main__":
    unittest.main()
