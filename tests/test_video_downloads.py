from __future__ import annotations

import hashlib
import io
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib.client import AsynxClient
from asxlib.errors import AsxError
from asxlib.media_results import attach_media_details, media_file_info

# Container-only fixture: metadata helpers deliberately do not decode video frames.
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp42" + b"\x00\x00\x00\x0cmdatdata"


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {"Content-Type": "video/mp4", "Content-Length": str(len(data))}
        self.read_sizes = []

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("unbounded read")
        self.read_sizes.append(size)
        return super().read(size)


class VideoDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / "download.part"
        self.client = AsynxClient("https://asynx.example", "test-key")

    def download(self, responses, **kwargs):
        with (
            patch("asxlib.client.build_opener") as factory,
            patch("asxlib.client.time.sleep"),
        ):
            factory.return_value.open.side_effect = responses
            return self.client.asset_to_file("task", 0, self.path, **kwargs)

    def test_streams_file_in_bounded_chunks_and_reports_metrics(self):
        data = MP4 + b"a" * 200_000
        response = Response(data)
        metrics = {}
        content_type = self.download([response], metrics=metrics)
        self.assertEqual(content_type, "video/mp4")
        self.assertEqual(self.path.read_bytes(), data)
        self.assertEqual(metrics["bytes"], len(data))
        self.assertTrue(all(size <= 64 * 1024 for size in response.read_sizes))
        self.assertEqual(media_file_info(self.path)["sha256"], hashlib.sha256(data).hexdigest())

    def test_retry_truncates_previous_partial_response(self):
        partial = Response(MP4 + b"partial", {"Content-Length": str(len(MP4) + 100)})
        self.download([partial, Response(MP4)])
        self.assertEqual(self.path.read_bytes(), MP4)

    def test_truncated_responses_fail_and_remove_temporary_file(self):
        responses = [Response(MP4, {"Content-Length": str(len(MP4) + 1)}) for _ in range(4)]
        with self.assertRaises(AsxError) as caught:
            self.download(responses)
        self.assertEqual(caught.exception.code, "asset_download_failed")
        self.assertFalse(self.path.exists())

    def test_oversized_declared_or_streamed_response_is_removed(self):
        for response in (
            Response(MP4, {"Content-Length": "1000000"}),
            Response(MP4 + b"x" * 100, {"Content-Type": "video/mp4"}),
        ):
            with self.subTest(headers=response.headers):
                with self.assertRaises(AsxError) as caught:
                    self.download([response], max_bytes=len(MP4) + 1)
                self.assertEqual(caught.exception.code, "response_too_large")
                self.assertFalse(self.path.exists())

    def test_fake_video_html_and_ftyp_only_are_rejected(self):
        for data in (b"<html>Access denied</html>", MP4[:24], b"1234ftypnot-a-real-video"):
            with self.subTest(data=data):
                with self.assertRaises(AsxError) as caught:
                    self.download([Response(data)])
                self.assertEqual(caught.exception.code, "unsupported_media_format")
                self.assertFalse(self.path.exists())

    def test_keyboard_interrupt_removes_partial_download(self):
        class Interrupted(Response):
            def read(self, size=-1):
                if self.tell():
                    raise KeyboardInterrupt
                return super().read(12)

        with self.assertRaises(KeyboardInterrupt):
            self.download([Interrupted(MP4)])
        self.assertFalse(self.path.exists())

    def test_video_and_poster_metadata_do_not_invent_video_properties(self):
        self.path.write_bytes(MP4)
        poster = self.root / "poster.png"
        Image.new("RGB", (12, 8), "red").save(poster)
        payload = {}
        attach_media_details(payload, [str(self.path), str(poster)], expected_ratio="16:9")
        self.assertEqual(len(payload["media"]), 2)
        self.assertEqual(len(payload["videos"]), 1)
        self.assertEqual(len(payload["images"]), 1)
        self.assertEqual(payload["videos"][0]["path"], str(self.path))
        for field in ("width", "height", "duration", "has_audio"):
            self.assertNotIn(field, payload["videos"][0])
        self.assertNotIn("warnings", payload)

    def test_missing_file_metadata_keeps_path_in_warning(self):
        payload = {}
        attach_media_details(payload, [str(self.path)])
        self.assertEqual(payload["videos"], [])
        self.assertEqual(payload["warnings"][0]["path"], str(self.path))

    def test_image_models_and_video_models_are_available_to_callers(self):
        items = [{"name": "image", "modality": "image"}, {"name": "MiniMax-H3", "modality": "video"}]
        with patch.object(self.client, "request_json", return_value=({"items": items}, None)):
            self.assertEqual(self.client.models()[0], items)

    def test_unsafe_direct_url_is_rejected(self):
        with self.assertRaises(AsxError) as caught:
            self.download([], download_url="file:///tmp/video.mp4")
        self.assertEqual(caught.exception.code, "invalid_asset_url")

    def test_cross_origin_redirect_drops_credentials(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            target = None

            def log_message(self, format, *args):
                pass

            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", Handler.target)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(MP4)))
                self.end_headers()
                self.wfile.write(MP4)

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), Handler) for _ in range(2)]
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        try:
            origin, target = [f"http://127.0.0.1:{server.server_address[1]}" for server in servers]
            Handler.target = target + "/video"
            client = AsynxClient(origin, "test-key")
            client.asset_to_file("task", 0, self.path, origin + "/redirect")
            self.assertEqual(self.path.read_bytes(), MP4)
            self.assertEqual(requests, [("/redirect", "Bearer test-key"), ("/video", None)])
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
