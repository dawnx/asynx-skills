from __future__ import annotations

import getpass
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .constants import DEFAULT_BASE_URL
from .errors import AsxError
from .output import log


def config_path() -> Path:
    override = os.environ.get("ASYNX_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Asynx" / "config.json"
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "asynx" / "config.json"


def state_path() -> Path:
    override = os.environ.get("ASYNX_STATE_PATH")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Asynx" / "state.db"
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return state_home / "asx" / "state.db"


def cache_path() -> Path:
    override = os.environ.get("ASYNX_CACHE_PATH")
    if override:
        return Path(override).expanduser()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "Asynx" / "cache" / "models.json"
    cache_home = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return cache_home / "asx" / "models.json"


def read_config() -> dict[str, Any]:
    path = config_path()
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AsxError(
            f"Cannot read Asynx configuration at {path}: {exc}",
            code="invalid_config",
        ) from exc
    if not isinstance(value, dict):
        raise AsxError(f"Asynx configuration at {path} must be a JSON object", code="invalid_config")
    return value


def validate_base_url(value: str) -> str:
    candidate = value.strip().rstrip("/")
    parts = urlsplit(candidate)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise AsxError("The Asynx Base URL is invalid", code="invalid_base_url")
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parts.scheme != "https" and parts.hostname not in local_hosts:
        raise AsxError(
            "The Asynx Base URL must use HTTPS except for localhost development",
            code="insecure_base_url",
        )
    return candidate


def validate_api_key(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or any(char.isspace() for char in value):
        raise AsxError(
            "Asynx API key is not configured; run the configure command in your terminal",
            code="missing_api_key",
        )
    normalized = value.strip()
    if not normalized.startswith("asx-"):
        raise AsxError("Asynx API keys must start with 'asx-'", code="invalid_api_key")
    return normalized


def load_credentials() -> tuple[str, str]:
    config = read_config()
    api_key = os.environ.get("ASYNX_API_KEY") or config.get("api_key")
    base_url = os.environ.get("ASYNX_BASE_URL") or config.get("base_url") or DEFAULT_BASE_URL
    if not isinstance(base_url, str):
        raise AsxError("The configured Asynx Base URL must be a string", code="invalid_base_url")
    return validate_api_key(api_key), validate_base_url(base_url)


def save_config(api_key: str, base_url: str) -> Path:
    path = config_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps({"api_key": api_key, "base_url": base_url}, indent=2) + "\n"
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix="config-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(payload)
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise AsxError(f"Cannot save Asynx configuration at {path}: {exc}", code="config_write_failed") from exc
    return path


def configure(base_url_override: str | None = None) -> dict[str, Any]:
    if not sys.stdin.isatty():
        raise AsxError("configure requires an interactive terminal", code="interactive_terminal_required")
    current = read_config()
    configured_url = current.get("base_url") if isinstance(current.get("base_url"), str) else None
    base_url = validate_base_url(
        base_url_override or os.environ.get("ASYNX_BASE_URL") or configured_url or DEFAULT_BASE_URL
    )
    log(f"Configuration: {config_path()}")
    log(f"Base URL: {base_url}")
    if base_url == DEFAULT_BASE_URL:
        log("Create an API key at https://asynx.llmapi.site/api-keys")
    log("Required scopes: tasks:read and tasks:write")
    existing_key = current.get("api_key") if isinstance(current.get("api_key"), str) else ""
    label = "API key (press Enter to keep the existing key): " if existing_key else "API key: "
    entered_key = getpass.getpass(label).strip()
    api_key = validate_api_key(entered_key or existing_key)
    path = save_config(api_key, base_url)
    return {
        "ok": True,
        "configured": True,
        "base_url": base_url,
        "config_path": str(path.resolve()),
    }
