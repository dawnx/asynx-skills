from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import batches, state, uploads
from asxlib.errors import AsxError
from test_audit_regressions import FakeClient


class MissingFileClient(FakeClient):
    """Refuses the first ``refusals`` submissions the way the service does for a lost file."""

    def __init__(self, refusals):
        super().__init__()
        self.refusals = refusals
        self.attempts = []

    def submit(self, body, key):
        self.attempts.append((body, key))
        if len(self.attempts) <= self.refusals:
            raise AsxError(
                "file is gone", code="file_not_found", http_status=404, task_created=False,
                next_step="change_request", details={"file_id": body["input"]["reference_images"][0]},
            )
        return super().submit(body, key)


class UploadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {
            "ASYNX_STATE_PATH": str(self.root / "state.db"),
            "ASYNX_CACHE_PATH": str(self.root / "cache" / "models.json"),
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.client = FakeClient()
        self.token = uploads.stage(b"\x89PNG\r\n\x1a\n prepared reference")
        self.body = {"task_type": "image.generate", "input": {"prompt": "cat", "reference_images": [self.token]}}

    def sent_reference(self, client, index=-1):
        return client.submitted[index][0]["input"]["reference_images"][0]

    def test_batch_items_share_one_upload_and_stored_rows_hold_only_the_token(self):
        # Every item used to carry its own copy of the image, in the request and in the ledger.
        source = self.root / "source.png"
        Image.new("RGB", (40, 30), "red").save(source)
        created = batches.create_batch(
            self.client, operation="generate", prompt="cat", model_selector=None,
            image_size=None, aspect_ratio=None, quality=None, count=1, output_format="png",
            references=[str(source)], mask=None, total=3, name=None, output_dir=str(self.root / "out"),
        )

        batches.poll_batch(self.client, created["id"], submissions_limit=3)

        self.assertEqual(len(self.client.uploaded), 1)
        sent = {body["input"]["reference_images"][0] for body, _key in self.client.submitted}
        self.assertEqual(len(self.client.submitted), 3)
        self.assertEqual(len(sent), 1)
        self.assertTrue(sent.pop().startswith("file_"))
        with state.connect_db() as connection:
            template = connection.execute("SELECT template_json FROM batches").fetchone()[0]
            stored = [template] + [
                row[0] for query in ("SELECT body_json FROM batch_items", "SELECT request_json FROM tasks")
                for row in connection.execute(query)
            ]
        token = json.loads(template)["input"]["reference_images"][0]
        self.assertEqual(uploads.read(token), self.client.uploaded[0])
        for text in stored:
            self.assertNotIn("file_", text)
            self.assertNotIn("base64", text)
        for request in stored[-3:]:
            self.assertEqual(json.loads(request)["input"]["reference_images"], [token])

    def test_upload_close_to_expiry_is_replaced_before_the_request_is_sent(self):
        # A file that expires between upload and submission makes a paid request fail.
        first = uploads.materialize(self.client, self.body)["input"]["reference_images"][0]
        self.assertEqual(uploads.materialize(self.client, self.body)["input"]["reference_images"], [first])
        with state.connect_db() as connection:
            connection.execute("UPDATE uploads SET expires_at = ?", (int(time.time()) + 300,))
            connection.commit()

        second = uploads.materialize(self.client, self.body)["input"]["reference_images"][0]

        self.assertNotEqual(second, first)
        self.assertEqual(len(self.client.uploaded), 2)
        self.assertEqual(self.body["input"]["reference_images"], [self.token])

    def test_lost_file_is_uploaded_again_and_resubmitted_once_under_the_same_key(self):
        # The service may drop a file early; one fresh upload must rescue the request,
        # and nothing may be sent again once an earlier attempt could still arrive.
        recovering = MissingFileClient(refusals=1)
        task, _request_id = uploads.submit(recovering, self.body, "asx-key")
        self.assertEqual(task["id"], "task_1")
        self.assertEqual([key for _body, key in recovering.attempts], ["asx-key", "asx-key"])
        self.assertEqual(len(recovering.uploaded), 2)
        self.assertNotEqual(*(body["input"]["reference_images"][0] for body, _key in recovering.attempts))

        for client, settled, attempts in (
            (MissingFileClient(refusals=2), True, 2), (MissingFileClient(refusals=1), False, 1),
        ):
            uploads.forget(client, self.body)
            with self.subTest(settled=settled), self.assertRaises(AsxError) as caught:
                uploads.submit(client, self.body, "asx-other", settled=settled)
            self.assertEqual(caught.exception.code, "file_not_found")
            self.assertEqual(len(client.attempts), attempts)
            self.assertEqual(client.submitted, [])

    def test_file_uploaded_with_one_key_or_service_is_never_sent_with_another(self):
        # Files belong to the account that uploaded them; another account gets file_not_found.
        own = uploads.materialize(self.client, self.body)["input"]["reference_images"][0]
        other_key = FakeClient()
        other_key.cache_scope = "b" * 64
        other_service = FakeClient()
        other_service.base_url = "https://other.example.test"

        for client in (other_key, other_service):
            with self.subTest(base_url=client.base_url):
                self.assertEqual(client.uploaded, [])
                uploads.materialize(client, self.body)
                self.assertEqual(len(client.uploaded), 1)
        self.assertEqual(uploads.materialize(self.client, self.body)["input"]["reference_images"], [own])
        self.assertEqual(len(self.client.uploaded), 1)

    def test_request_whose_local_image_is_gone_is_refused_before_anything_is_sent(self):
        # A missing local copy must not turn into a request without its reference image.
        (uploads.uploads_dir() / self.token.removeprefix(uploads.TOKEN_PREFIX)).unlink()
        with self.assertRaises(AsxError) as caught:
            uploads.submit(self.client, self.body, "asx-key")
        self.assertEqual(caught.exception.code, "reference_image_cache_missing")
        self.assertTrue(caught.exception.submission_rejected)
        self.assertEqual(self.client.submitted + self.client.uploaded, [])


if __name__ == "__main__":
    unittest.main()
