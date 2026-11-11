from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import AsxError, AsynxClient
from asxlib.cli import execute, parser


class ModelGuideTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.status = 200
        self.response = {
            "code": "ok", "request_id": "guide-request",
            "data": {
                "model": "provider/model ?", "revision": "revision-1",
                "status": "documented", "content": "服务生成约束\n人工使用建议",
            },
        }
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner.requests.append((self.path, self.headers.get("Authorization")))
                body = json.dumps(owner.response).encode()
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.client = AsynxClient(f"http://127.0.0.1:{self.server.server_port}", "asx-guide-test")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_describe_uses_encoded_path_and_only_requests_examples_when_explicit(self):
        for examples in (False, True):
            self.requests.clear()
            arguments = ["models", "describe", "--model", "provider/model ?"]
            if examples:
                arguments.append("--examples")
            with self.subTest(examples=examples), patch("asxlib.cli._client", return_value=self.client):
                result, code = execute(parser().parse_args(arguments))
                self.assertEqual(code, 0)
                self.assertEqual(result, {"ok": True, **self.response["data"], "request_id": "guide-request"})
                self.assertNotIn("asx-guide-test", json.dumps(result))
                suffix = "?examples=true" if examples else ""
                self.assertEqual(self.requests, [
                    ("/v1/tasks/models/provider%2Fmodel%20%3F/guide" + suffix, "Bearer asx-guide-test"),
                ])

    def test_client_preserves_complete_manual_content_and_only_returns_guide_fields(self):
        text = "人工说明和示例\n" * 10000
        self.response["data"].update(content=text, internal="not-public")
        for kwargs, suffix in [({}, ""), ({"examples": False}, ""), ({"examples": True}, "?examples=true")]:
            self.requests.clear()
            with self.subTest(kwargs=kwargs):
                guide, _ = self.client.model_guide("name?examples=false", **kwargs)
                self.assertEqual(set(guide), {"model", "revision", "status", "content"})
                self.assertEqual(guide["content"], text)
                self.assertEqual(self.requests[0][0], "/v1/tasks/models/name%3Fexamples%3Dfalse/guide" + suffix)

    def test_unconfirmed_and_invalid_guides_do_not_become_documented(self):
        self.response["data"]["status"] = "unconfirmed"
        guide, _ = self.client.model_guide("unknown")
        self.assertEqual(guide["status"], "unconfirmed")
        self.response["data"].pop("content")
        with self.assertRaises(AsxError) as raised:
            self.client.model_guide("unknown")
        self.assertEqual(raised.exception.code, "invalid_model_guide")

    def test_forbidden_and_missing_guide_remain_errors_without_catalog_fallback(self):
        for status, code in [(403, "forbidden"), (404, "model_not_found")]:
            with self.subTest(status=status):
                self.requests.clear()
                self.status = status
                self.response = {"code": code, "message": "raw upstream details"}
                with self.assertRaises(AsxError) as raised:
                    self.client.model_guide("restricted")
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(raised.exception.http_status, status)
                self.assertNotIn("raw upstream details", str(raised.exception))
                self.assertEqual(len(self.requests), 1)

    def test_legacy_models_flags_still_dispatch_to_listing(self):
        for arguments, operation, modality in [
            (["models"], "all", "image"),
            (["models", "--operation", "edit", "--modality", "all"], "edit", "all"),
        ]:
            with self.subTest(arguments=arguments), patch("asxlib.cli._client", return_value=self.client), patch(
                "asxlib.cli.models_payload", return_value={"ok": True, "models": []}
            ) as listing:
                result, code = execute(parser().parse_args(arguments))
                listing.assert_called_once_with(self.client, operation, modality=modality)
                self.assertEqual((result, code), ({"ok": True, "models": []}, 0))


if __name__ == "__main__":
    unittest.main()
