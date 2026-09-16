from __future__ import annotations

import getpass
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .constants import DEFAULT_BASE_URL, VERSION
from .errors import AsxError
from .output import log


def config_path() -> Path:
    """Return the stable, user-level configuration path."""
    override = os.environ.get("ASYNX_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        app_data = os.environ.get("APPDATA")
        root = Path(app_data).expanduser() if app_data else Path.home() / "AppData" / "Roaming"
        return root / "Asynx" / "config.json"
    return Path.home() / ".config" / "asynx" / "config.json"


def legacy_config_paths() -> list[Path]:
    """Return discoverable paths used by releases that honored XDG_CONFIG_HOME."""
    if os.environ.get("ASYNX_CONFIG_PATH"):
        return []
    xdg_home = os.environ.get("XDG_CONFIG_HOME")
    if not xdg_home:
        return []
    legacy = Path(xdg_home).expanduser() / "asynx" / "config.json"
    canonical = config_path()
    return [] if legacy == canonical else [legacy]


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


def _read_config_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AsxError(
            f"Cannot read Asynx configuration at {path}: {exc}",
            code="invalid_config",
            details={"config_path": str(path)},
        ) from exc
    if not isinstance(value, dict):
        raise AsxError(
            f"Asynx configuration at {path} must be a JSON object",
            code="invalid_config",
            details={"config_path": str(path)},
        )
    return value


def read_config_with_source() -> tuple[dict[str, Any], Path | None]:
    canonical = config_path()
    if canonical.exists():
        return _read_config_file(canonical), canonical
    for path in legacy_config_paths():
        if path.exists():
            return _read_config_file(path), path
    return {}, None


def read_config() -> dict[str, Any]:
    return read_config_with_source()[0]


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


def _configure_command() -> str:
    script = Path(__file__).resolve().parents[1] / "asynx.py"
    if os.name == "nt":
        return f'py "{script}" configure'
    return f"python3 {shlex.quote(str(script))} configure"


def _checked_config_paths() -> list[Path]:
    return [config_path(), *legacy_config_paths()]


def validate_api_key(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or any(char.isspace() for char in value):
        canonical = config_path()
        command = _configure_command()
        checked = [str(path) for path in _checked_config_paths()]
        raise AsxError(
            "Asynx API key is not configured. "
            f"Checked: {', '.join(checked)}. Run in your terminal: {command}",
            code="missing_api_key",
            details={
                "config_path": str(canonical),
                "checked_paths": checked,
                "configure_command": command,
                "credential_source": "none",
            },
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


def _write_config(payload: dict[str, Any], path: Path) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = json.dumps(payload, indent=2) + "\n"
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
            handle.write(encoded)
        os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as exc:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise AsxError(
            f"Cannot save Asynx configuration at {path}: {exc}",
            code="config_write_failed",
            details={"config_path": str(path)},
        ) from exc
    return path


def save_config(api_key: str, base_url: str) -> Path:
    return _write_config({"api_key": api_key, "base_url": base_url}, config_path())


def migrate_legacy_config() -> Path | None:
    """Copy a discoverable legacy config to the canonical path without deleting it."""
    canonical = config_path()
    if canonical.exists() or os.environ.get("ASYNX_CONFIG_PATH"):
        return None
    for legacy in legacy_config_paths():
        if legacy.exists():
            _write_config(_read_config_file(legacy), canonical)
            return canonical
    return None


def _masked_key(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.startswith("asx-"):
        return f"asx-{normalized[4:8]}..." if len(normalized) > 8 else "asx-****"
    return "<invalid>"


def config_status() -> dict[str, Any]:
    config, source = read_config_with_source()
    env_key = os.environ.get("ASYNX_API_KEY")
    stored_key = config.get("api_key")
    key = env_key or stored_key
    credential_source = "environment" if env_key else "config_file" if stored_key else "none"
    base_url = os.environ.get("ASYNX_BASE_URL") or config.get("base_url") or DEFAULT_BASE_URL
    normalized_base_url: str | None = None
    base_url_valid = False
    if isinstance(base_url, str):
        try:
            normalized_base_url = validate_base_url(base_url)
            base_url_valid = True
        except AsxError:
            normalized_base_url = "<invalid>"
    overrides = [
        name
        for name in ("ASYNX_API_KEY", "ASYNX_BASE_URL", "ASYNX_CONFIG_PATH")
        if os.environ.get(name)
    ]
    canonical = config_path()
    return {
        "ok": True,
        "configured": isinstance(key, str) and bool(key.strip()),
        "credential_source": credential_source,
        "config_path": str(canonical),
        "loaded_config_path": str(source) if source else None,
        "config_exists": canonical.exists(),
        "api_key_prefix": _masked_key(key),
        "key_prefix_valid": isinstance(key, str) and key.strip().startswith("asx-"),
        "base_url": normalized_base_url,
        "base_url_valid": base_url_valid,
        "skill_version": VERSION,
        "environment_overrides": overrides,
    }


def _installation_info() -> list[dict[str, Any]]:
    current = Path(__file__).resolve().parents[2]
    candidates = [
        current,
        Path.home() / ".agents" / "skills" / "asx",
        Path.home() / ".claude" / "skills" / "asx",
        Path.home() / ".codex" / "skills" / "asx",
    ]
    installations: list[dict[str, Any]] = []
    seen: set[Path] = set()
    pattern = re.compile(r'^VERSION\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved in seen or not candidate.is_dir():
            continue
        seen.add(resolved)
        version_file = candidate / "scripts" / "asxlib" / "constants.py"
        version: str | None = None
        try:
            match = pattern.search(version_file.read_text(encoding="utf-8"))
            version = match.group(1) if match else None
        except OSError:
            pass
        installations.append(
            {"path": str(resolved), "version": version, "current": resolved == current.resolve()}
        )
    return installations


def _permission_check(path: Path | None) -> tuple[bool, str]:
    if path is None or not path.exists():
        return True, "No configuration file is currently loaded"
    if os.name == "nt":
        return True, "File mode checks are not available on Windows"
    mode = stat.S_IMODE(path.stat().st_mode)
    secure = mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    return secure, f"{path} mode is {mode:04o}"


def doctor(*, verify: bool = False) -> dict[str, Any]:
    status = config_status()
    source_value = status["loaded_config_path"]
    source = Path(source_value) if isinstance(source_value, str) else None
    permission_ok, permission_message = _permission_check(source)
    installations = _installation_info()
    versions = {item["version"] for item in installations if item["version"]}
    version_ok = len(versions) <= 1
    existing_configs = [str(path) for path in _checked_config_paths() if path.exists()]
    config_count_ok = len(existing_configs) <= 1
    checks: list[dict[str, Any]] = [
        {
            "name": "credentials",
            "ok": status["configured"] and status["key_prefix_valid"],
            "message": f"Credential source: {status['credential_source']}",
        },
        {"name": "config_permissions", "ok": permission_ok, "message": permission_message},
        {
            "name": "base_url",
            "ok": status["base_url_valid"],
            "message": "Base URL is valid"
            if status["base_url_valid"]
            else "Base URL is invalid",
        },
        {
            "name": "config_files",
            "ok": config_count_ok,
            "message": "At most one configuration file was found"
            if config_count_ok
            else "Multiple configuration files were found",
            "details": {"paths": existing_configs},
        },
        {
            "name": "skill_versions",
            "ok": version_ok,
            "message": "Installed skill copies use the same version"
            if version_ok
            else "Installed skill copies use different versions",
        },
    ]
    issues: list[dict[str, str]] = []
    if not status["configured"]:
        issues.append(
            {
                "level": "error",
                "code": "missing_api_key",
                "message": f"Configure Asynx in a terminal with: {_configure_command()}",
            }
        )
    elif not status["key_prefix_valid"]:
        issues.append(
            {
                "level": "error",
                "code": "invalid_api_key",
                "message": "The configured API key does not start with 'asx-'",
            }
        )
    if not status["base_url_valid"]:
        issues.append(
            {
                "level": "error",
                "code": "invalid_base_url",
                "message": "The configured Asynx Base URL is invalid",
            }
        )
    if not permission_ok:
        issues.append(
            {
                "level": "warning",
                "code": "insecure_config_permissions",
                "message": permission_message,
            }
        )
    if not version_ok:
        issues.append(
            {
                "level": "warning",
                "code": "skill_version_mismatch",
                "message": "Codex and Claude may be using different Asynx skill versions",
            }
        )
    if not config_count_ok:
        issues.append(
            {
                "level": "warning",
                "code": "multiple_config_files",
                "message": "Multiple Asynx configuration files exist; the canonical file takes precedence",
            }
        )

    result: dict[str, Any] = {
        "ok": not any(issue["level"] == "error" for issue in issues),
        "skill_version": VERSION,
        "python_executable": sys.executable,
        "home": str(Path.home()),
        "config": status,
        "checks": checks,
        "issues": issues,
        "installations": installations,
    }
    if verify:
        try:
            from .client import AsynxClient

            api_key, base_url = load_credentials()
            models, request_id = AsynxClient(base_url, api_key).models()
            capable = [
                model
                for model in models
                if {"image.generate", "image.edit"}.intersection(model.get("task_types", []))
            ]
            if not capable:
                raise AsxError(
                    "No image models are currently available",
                    code="no_image_models",
                    exit_code=3,
                )
            verification: dict[str, Any] = {
                "ok": True,
                "image_model_count": len(capable),
                "request_id": request_id,
            }
        except AsxError as exc:
            verification = {"ok": False, "error": exc.payload()["error"]}
            result["ok"] = False
            issues.append(
                {
                    "level": "error",
                    "code": "api_verification_failed",
                    "message": exc.message,
                }
            )
        result["verification"] = verification
        checks.append(
            {
                "name": "api_verification",
                "ok": verification["ok"],
                "message": "Asynx API verification succeeded"
                if verification["ok"]
                else "Asynx API verification failed",
            }
        )
    return result


def configure(base_url_override: str | None = None) -> dict[str, Any]:
    if not sys.stdin.isatty():
        raise AsxError("configure requires an interactive terminal", code="interactive_terminal_required")
    migrated = migrate_legacy_config()
    current = read_config()
    configured_url = current.get("base_url") if isinstance(current.get("base_url"), str) else None
    base_url = validate_base_url(
        base_url_override or os.environ.get("ASYNX_BASE_URL") or configured_url or DEFAULT_BASE_URL
    )
    log(f"Configuration: {config_path()}")
    if migrated:
        log(f"Migrated legacy configuration to: {migrated}")
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
