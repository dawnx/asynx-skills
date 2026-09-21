from __future__ import annotations

from datetime import timezone

VERSION = "0.4.0"
UTC = timezone.utc
DEFAULT_BASE_URL = "https://asynx.llmapi.site/api"
DEFAULT_MODEL = "gpt-image-2"
DEFAULT_OUTPUT_DIR = "generated-images"
DEFAULT_BATCH_SUBMISSIONS_PER_POLL = 4
MODEL_CACHE_TTL_SECONDS = 300
MAX_TASK_POLL_DELAY_SECONDS = 4.0
MAX_BATCH_ITEMS = 1000
REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 120
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ASSET_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_HTTP_ATTEMPTS = 4
MAX_REDIRECTS = 5
MAX_REFERENCE_IMAGES = 5
MAX_REFERENCE_SOURCE_BYTES = 25 * 1024 * 1024
MAX_REFERENCE_BYTES = 5 * 1024 * 1024
MAX_REFERENCE_TOTAL_BYTES = 20 * 1024 * 1024
REFERENCE_NETWORK_WARNING_BYTES = 1024 * 1024
MAX_REFERENCE_DIMENSION = 1600
MAX_REFERENCE_PIXELS = 2_560_000
MAX_SAFE_IMAGE_PIXELS = 40 * 1024 * 1024
MAX_MASK_BYTES = 10 * 1024 * 1024
MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024
REFERENCE_WEBP_QUALITY = 82
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
