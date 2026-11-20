from __future__ import annotations

from datetime import timezone

VERSION = "0.7.2"
UTC = timezone.utc
DEFAULT_BASE_URL = "https://asynx.llmapi.site/api"
# How a request that names no model is recorded locally until the service
# reports which model it used. Which model that is, is the service's decision.
SERVICE_DEFAULT_MODEL = "default"
DEFAULT_OUTPUT_DIR = "generated-images"
DEFAULT_BATCH_SUBMISSIONS_PER_POLL = 4
MODEL_CACHE_TTL_SECONDS = 300
# How long one status request asks the service to hold on for a change (the service allows 60).
TASK_WAIT_SECONDS = 30
MAX_BATCH_ITEMS = 1000
REQUEST_TIMEOUT_SECONDS = 30
DOWNLOAD_TIMEOUT_SECONDS = 120
# An upload is given at least this throughput before it times out.
MIN_UPLOAD_BYTES_PER_SECOND = 256 * 1024
# The service's limit for one uploaded local 3D model.
MAX_MODEL_SOURCE_BYTES = 150_000_000
MAX_API_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ASSET_RESPONSE_BYTES = 50 * 1024 * 1024
MAX_HTTP_ATTEMPTS = 4
MAX_REDIRECTS = 5
MAX_REFERENCE_IMAGES = 16
MAX_REFERENCE_SOURCE_BYTES = 25 * 1024 * 1024
# The service's limits for one image input and for all of a task's inputs.
MAX_REFERENCE_BYTES = 10 * 1024 * 1024
MAX_REFERENCE_TOTAL_BYTES = 20 * 1024 * 1024
# Large enough for a 4K result, so editing one does not shrink it first.
MAX_REFERENCE_DIMENSION = 3840
MAX_REFERENCE_PIXELS = 8_294_400
MAX_SAFE_IMAGE_PIXELS = 40 * 1024 * 1024
MAX_MASK_BYTES = 10 * 1024 * 1024
# Lossy encoding only when a lossless image would not fit the byte budget.
REFERENCE_WEBP_QUALITY = 90
# The last resort keeps the earlier small, lossy form, which always fits.
COMPACT_REFERENCE_DIMENSION = 1600
COMPACT_REFERENCE_PIXELS = 2_560_000
COMPACT_REFERENCE_WEBP_QUALITY = 82
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
