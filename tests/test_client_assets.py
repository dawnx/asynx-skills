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


class AssetHandler(BaseHTTPRequestHandler):
    requests: ClassVar[list[tuple[str, str | None]]] = []
    redirect_target: ClassVar[str | None] = None

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        type(self).requests.append((self.path, self.headers.get("Authorization")))
        target = type(self).redirect_target
        if self.path == "/redirect" and target:
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()
            return
        body = b"asset-bytes"
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class AssetClientTestCase(unittest.TestCase):
    def setUp(self) -> None:
        AssetHandler.requests = []
        AssetHandler.redirect_target = None
        self.servers: list[ThreadingHTTPServer] = []
        self.threads: list[threading.Thread] = []

    def tearDown(self) -> None:
        for server in self.servers:
            server.shutdown()
            server.server_close()
        for thread in self.threads:
            thread.join(timeout=2)

    def _start_server(self) -> str:
        server = ThreadingHTTPServer(("127.0.0.1", 0), AssetHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.servers.append(server)
        self.threads.append(thread)
        host, port = server.server_address[:2]
        host_text = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{int(port)}"

    def test_default_endpoint_keeps_authorization(self) -> None:
        base_url = self._start_server()
        client = AsynxClient(base_url, "asx-test-key")
        metrics: dict[str, Any] = {}

        body, content_type = client.asset("task/one", 0, metrics=metrics)

        self.assertEqual(body, b"asset-bytes")
        self.assertEqual(content_type, "image/png")
        self.assertEqual(metrics["source"], "asset_endpoint")
        self.assertIsNone(metrics["download_url"])
        self.assertGreaterEqual(metrics["ttfb_seconds"], 0.0)
        self.assertGreaterEqual(metrics["transfer_seconds"], 0.0)
        self.assertEqual(metrics["bytes"], len(body))
        self.assertEqual(
            AssetHandler.requests,
            [("/v1/tasks/task%2Fone/assets/0", "Bearer asx-test-key")],
        )

    def test_cross_origin_download_url_does_not_send_authorization(self) -> None:
        base_url = self._start_server()
        download_url = self._start_server() + "/asset.png"
        client = AsynxClient(base_url, "asx-test-key")
        metrics: dict[str, Any] = {}

        body, _content_type = client.asset("task", 0, download_url, metrics=metrics)

        self.assertEqual(body, b"asset-bytes")
        self.assertEqual(metrics["source"], "download_url")
        self.assertEqual(metrics["download_url"], download_url)
        self.assertEqual(metrics["bytes"], len(body))
        self.assertEqual(AssetHandler.requests, [("/asset.png", None)])

    def test_cross_origin_redirect_does_not_leak_authorization(self) -> None:
        base_url = self._start_server()
        target_url = self._start_server() + "/asset.png"
        AssetHandler.redirect_target = target_url
        client = AsynxClient(base_url, "asx-test-key")

        body, _content_type = client.asset("task", 0, base_url + "/redirect")

        self.assertEqual(body, b"asset-bytes")
        self.assertEqual(
            AssetHandler.requests,
            [("/redirect", "Bearer asx-test-key"), ("/asset.png", None)],
        )

    def test_download_url_rejects_unsafe_urls(self) -> None:
        base_url = self._start_server()
        client = AsynxClient(base_url, "asx-test-key")

        for download_url in (
            "file:///tmp/image.png",
            "ftp://example.com/image.png",
            "http://user:pass@example.com/image.png",
            "http://example.com/image.png#fragment",
            " http://example.com/image.png",
        ):
            with self.subTest(download_url=download_url):
                with self.assertRaises(AsxError) as caught:
                    client.asset("task", 0, download_url)
                self.assertEqual(caught.exception.code, "invalid_asset_url")

        self.assertEqual(AssetHandler.requests, [])

    def test_redirect_rejects_unsafe_location(self) -> None:
        base_url = self._start_server()
        AssetHandler.redirect_target = "file:///tmp/image.png"
        client = AsynxClient(base_url, "asx-test-key")

        with self.assertRaises(AsxError) as caught:
            client.asset("task", 0, base_url + "/redirect")

        self.assertEqual(caught.exception.code, "invalid_asset_redirect")
        self.assertEqual(AssetHandler.requests, [("/redirect", "Bearer asx-test-key")])


if __name__ == "__main__":
    unittest.main()
