from __future__ import annotations

from datetime import timezone

VERSION = "0.1.0"
UTC = timezone.utc
DEFAULT_BASE_URL = "https://asynx.llmapi.site/api"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_OUTPUT_DIR = "generated-images"
DEFAULT_BATCH_SUBMISSIONS_PER_POLL = 4
MAX_BATCH_ITEMS = 1000
REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 120
MAX_HTTP_ATTEMPTS = 4
MAX_REDIRECTS = 5
MAX_IMAGE_INPUTS = 8
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "timeout", "canceled"})
KNOWN_STATUSES = TERMINAL_STATUSES | frozenset({"queued", "running", "delayed", "canceling"})
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})
LOCAL_ITEM_STATUSES = frozenset(
    {
        "pending",
        "submitting",
        "submitted",
        "queued",
        "running",
        "delayed",
        "canceling",
        "succeeded",
        "failed",
        "timeout",
        "canceled",
    }
)
