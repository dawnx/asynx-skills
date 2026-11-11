from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "skills/asynx-image/scripts"))

from asxlib import images
from asxlib.client import AsynxClient
from asxlib.errors import AsxError


class ModelCacheScopeTests(unittest.TestCase):
    def test_accounts_on_same_service_never_reuse_each_others_models_or_prices(self):
        first = AsynxClient("https://catalog.test", "asx-first-account-private")
        second = AsynxClient("https://catalog.test", "asx-second-account-private")
        old = [{"name": "private-model-1", "task_types": ["image.generate"],
                "pricing": {"mode": "per_request", "amount": "0.01"}}]
        current = [{"name": "public-model-2", "task_types": ["image.generate"],
                    "pricing": {"mode": "per_request", "amount": "0.20"}}]
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"ASYNX_CACHE_PATH": str(Path(directory) / "models.json")}
        ):
            images.cache_models(first.base_url, old, scope=first.cache_scope)
            self.assertEqual(images._cached_models(first.base_url, scope=first.cache_scope), old)
            self.assertIsNone(images._cached_models(second.base_url, scope=second.cache_scope))
            with patch.object(second, "models", return_value=(current, "second-request")) as catalog:
                with self.assertRaises(AsxError) as raised:
                    images.build_task(
                        second, task_type="image.generate", model_selector="private model",
                        prompt="cup", image_size=None, aspect_ratio=None, quality=None,
                        count=1, output_format="png", references=[],
                    )
                self.assertEqual(raised.exception.code, "model_unavailable")
                catalog.assert_called_once()
            self.assertEqual(images._cached_models(second.base_url, scope=second.cache_scope), current)
            raw = (Path(directory) / "models.json").read_text()
            self.assertNotIn(first.api_key, raw)
            self.assertNotIn(second.api_key, raw)
            self.assertNotIn("0.01", raw)

    def test_legacy_unscoped_catalog_is_ignored(self):
        client = AsynxClient("https://catalog.test", "asx-private")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"ASYNX_CACHE_PATH": str(Path(directory) / "models.json")}
        ):
            (Path(directory) / "models.json").write_text(json.dumps({
                "base_url": client.base_url, "fetched_at": time.time(),
                "items": [{"name": "old-account-model"}],
            }))
            self.assertIsNone(images._cached_models(client.base_url, scope=client.cache_scope))
            self.assertIsNone(images._cached_models(client.base_url))


if __name__ == "__main__":
    unittest.main()
