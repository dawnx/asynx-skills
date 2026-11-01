from __future__ import annotations

import getpass
import json
import math
import os
import re
import shlex
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .constants import DEFAULT_BASE_URL, DEFAULT_INITIAL_POLL_DELAY_SECONDS, VERSION
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
            f"配置文件读取失败，请检查文件权限与 JSON 格式：{path}",
            code="invalid_config",
            details={"config_path": str(path)},
        ) from exc
    if not isinstance(value, dict):
        raise AsxError(
            f"配置文件格式不正确，应为 JSON 对象：{path}",
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


def initial_poll_delay_seconds() -> float:
    raw = os.environ.get("ASYNX_INITIAL_POLL_DELAY_SECONDS")
    value = raw if raw is not None else read_config().get(
        "initial_poll_delay_seconds", DEFAULT_INITIAL_POLL_DELAY_SECONDS
    )
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise TypeError
        delay = float(value)
        if not math.isfinite(delay) or not 0 <= delay <= 300:
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise AsxError(
            "首次查询等待时间须为 0–300 之间的秒数，请检查 initial_poll_delay_seconds "
            "或环境变量 ASYNX_INITIAL_POLL_DELAY_SECONDS。",
            code="invalid_poll_delay",
        ) from exc
    return delay


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
        raise AsxError("服务地址格式不正确，请使用完整的 HTTP(S) 地址，不可包含密钥、查询参数或 # 片段。", code="invalid_base_url")
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parts.scheme != "https" and parts.hostname not in local_hosts:
        raise AsxError(
            "服务地址必须使用 HTTPS，仅本机调试地址可使用 HTTP。请检查服务地址配置。",
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
    if not isinstance(value, str) or not value.strip():
        canonical = config_path()
        command = _configure_command()
        checked = [str(path) for path in _checked_config_paths()]
        raise AsxError(
            f"尚未配置可用的 LLMAPI API Key。配置文件：{canonical}。"
            f"请在自己的终端运行以下命令输入密钥（请勿发送至对话）：{command}",
            code="missing_api_key",
            details={
                "config_path": str(canonical),
                "checked_paths": checked,
                "configure_command": command,
                "credential_source": "none",
            },
        )
    if any(char.isspace() for char in value):
        raise AsxError("API Key 包含空格或换行，请复制完整密钥后重试。", code="invalid_api_key")
    return value


def load_credentials() -> tuple[str, str]:
    config = read_config()
    api_key = os.environ.get("ASYNX_API_KEY") or config.get("api_key")
    base_url = os.environ.get("ASYNX_BASE_URL") or config.get("base_url") or DEFAULT_BASE_URL
    if not isinstance(base_url, str):
        raise AsxError("服务地址格式不正确，请将 base_url 设置为完整网址。", code="invalid_base_url")
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
            f"配置保存失败，请检查目录写入权限与剩余磁盘空间：{path}",
            code="config_write_failed",
            details={"config_path": str(path)},
        ) from exc
    return path


def save_config(api_key: str, base_url: str) -> Path:
    payload = read_config()
    payload.update({"api_key": api_key, "base_url": base_url})
    return _write_config(payload, config_path())


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
    if not isinstance(value, str) or not value.strip() or any(char.isspace() for char in value):
        return None
    normalized = value.strip()
    if len(normalized) <= 8:
        return "<redacted>"
    return f"{normalized[:4]}...{normalized[-4:]}"


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
        "key_valid": isinstance(key, str) and bool(key.strip()) and not any(char.isspace() for char in key),
        "base_url": normalized_base_url,
        "base_url_valid": base_url_valid,
        "skill_version": VERSION,
        "environment_overrides": overrides,
    }


def legacy_installation_notice() -> dict[str, Any] | None:
    """Inspect only fixed legacy entry paths; never mutate installations or state."""
    try:
        home = Path.home()
        candidates = [home / agent / "skills" / "asx" for agent in (".agents", ".claude", ".codex")]
        paths = [str(path) for path in candidates if path.exists() or path.is_symlink()]
    except (OSError, RuntimeError):
        # An optional cleanup reminder must not hide the command's actual result.
        return None
    if not paths:
        return None
    return {
        "code": "legacy_skill_installation",
        "paths": paths,
        "message": (
            "检测到旧版 asx 安装，Agent 可能调用到旧版本。可让 Agent 协助确认并备份旧版后重新安装；"
            "密钥、任务记录和已生成文件均可继续使用。确认前不会移动或删除旧版。"
        ),
    }


def _installation_info() -> list[dict[str, Any]]:
    current = Path(__file__).resolve().parents[2]
    candidates = [
        current,
        Path.home() / ".agents" / "skills" / "asynx-image",
        Path.home() / ".claude" / "skills" / "asynx-image",
        Path.home() / ".codex" / "skills" / "asynx-image",
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
        return True, "当前未加载配置文件。"
    if os.name == "nt":
        return True, "Windows 不适用此权限检查。"
    mode = stat.S_IMODE(path.stat().st_mode)
    secure = mode & (stat.S_IRWXG | stat.S_IRWXO) == 0
    return secure, (
        "配置文件权限正常。"
        if secure
        else f"配置文件对其他系统用户可读，请限制为仅当前用户可读写：{path}"
    )


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
            "ok": status["configured"] and status["key_valid"],
            "message": {
                "environment": "正在使用环境变量里的 API Key。",
                "config_file": "正在使用已保存的 API Key。",
                "none": "尚未配置 API Key。",
            }[status["credential_source"]],
        },
        {"name": "config_permissions", "ok": permission_ok, "message": permission_message},
        {
            "name": "base_url",
            "ok": status["base_url_valid"],
            "message": "服务地址格式正常。"
            if status["base_url_valid"]
            else "服务地址格式不正确，请检查地址配置。",
        },
        {
            "name": "config_files",
            "ok": config_count_ok,
            "message": "配置文件检查正常。"
            if config_count_ok
            else "检测到多份配置文件，请确认实际使用的一份。",
            "details": {"paths": existing_configs},
        },
        {
            "name": "skill_versions",
            "ok": version_ok,
            "message": "已发现的 Skill 安装版本一致。"
            if version_ok
            else "检测到不同版本的 Skill，请确认 Agent 实际调用的位置。",
        },
    ]
    issues: list[dict[str, str]] = []
    if not status["configured"]:
        issues.append(
            {
                "level": "error",
                "code": "missing_api_key",
                "message": f"尚未配置 API Key，请在自己的终端运行：{_configure_command()}。请勿将密钥发送至对话。",
            }
        )
    elif not status["key_valid"]:
        issues.append(
            {
                "level": "error",
                "code": "invalid_api_key",
                "message": "API Key 为空或包含空格、换行，请在自己的终端重新输入完整密钥。",
            }
        )
    if not status["base_url_valid"]:
        issues.append(
            {
                "level": "error",
                "code": "invalid_base_url",
                "message": "服务地址格式不正确，请检查 base_url 或环境变量 ASYNX_BASE_URL。",
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
                "message": "不同安装位置的 Asynx Skill 版本不一致，请确认实际调用的副本，并按需更新。",
            }
        )
    if not config_count_ok:
        issues.append(
            {
                "level": "warning",
                "code": "multiple_config_files",
                "message": "检测到多份 Asynx 配置文件，默认优先使用当前配置目录中的文件；请核对实际加载的路径。",
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
                if {"image.generate", "image.edit", "video.generate"}.intersection(model.get("task_types", []))
            ]
            if not capable:
                raise AsxError(
                    "连接正常，但当前没有可用的图片或视频模型。请稍后重试或联系服务支持。",
                    code="no_media_models",
                    exit_code=3,
                )
            verification: dict[str, Any] = {
                "ok": True,
                "image_model_count": sum(bool({"image.generate", "image.edit"}.intersection(model.get("task_types", []))) for model in capable),
                "video_model_count": sum("video.generate" in model.get("task_types", []) for model in capable),
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
                "message": "连接检查通过，已获取可用的图片或视频模型；本次未提交生成任务。"
                if verification["ok"]
                else "连接检查未通过，请按下列提示处理。",
            }
        )
    return result


def configure(base_url_override: str | None = None) -> dict[str, Any]:
    if not sys.stdin.isatty():
        raise AsxError(
            "请在自己的终端运行配置命令并输入 API Key，请勿发送至对话。",
            code="interactive_terminal_required",
        )
    migrated = migrate_legacy_config()
    current = read_config()
    configured_url = current.get("base_url") if isinstance(current.get("base_url"), str) else None
    base_url = validate_base_url(
        base_url_override or os.environ.get("ASYNX_BASE_URL") or configured_url or DEFAULT_BASE_URL
    )
    log(f"配置文件：{config_path()}")
    if migrated:
        log(f"旧配置已迁移至：{migrated}，原文件已保留。")
    if base_url != DEFAULT_BASE_URL:
        log(f"服务地址：{base_url}")
    if base_url == DEFAULT_BASE_URL:
        log(
            "尚未配置 API Key？请前往 https://llmapi.site/api-keys 创建。"
        )
    log("输入时不会显示字符，属于正常现象。")
    existing_key = current.get("api_key") if isinstance(current.get("api_key"), str) else ""
    label = "请输入 API Key（直接回车沿用已保存的密钥）：" if existing_key else "请输入 API Key："
    try:
        entered_key = getpass.getpass(label).strip()
    except (EOFError, KeyboardInterrupt):
        raise AsxError("已取消输入，未保存新密钥。", code="interrupted", exit_code=130) from None
    api_key = validate_api_key(entered_key or existing_key)
    path = save_config(api_key, base_url)
    log("API Key 已保存，尚未验证连接。")
    if os.environ.get("ASYNX_API_KEY"):
        log("注意：当前已设置 ASYNX_API_KEY 环境变量，运行时将优先使用它；如需改用刚保存的密钥，请先移除或更新该环境变量。")
    log("可返回 Agent 对话继续使用 Asynx，无需再次发送密钥。")
    return {
        "ok": True,
        "configured": True,
        "base_url": base_url,
        "config_path": str(path.resolve()),
    }
