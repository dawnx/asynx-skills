from __future__ import annotations

import json
import sys
from typing import Any


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)
