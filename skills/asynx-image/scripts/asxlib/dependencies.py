"""The one third-party dependency, Pillow, and the `setup` command that provides it.

No distribution channel runs an install step and each may replace the Skill's
files on update, so Pillow is installed into a per-user directory outside the
Skill, one per Python minor version because its wheels are built per version.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import config_status, configure_command, state_path
from .constants import VERSION
from .errors import AsxError

PILLOW_REQUIREMENT = "Pillow>=10.2,<13"
_PIP_TIMEOUT_SECONDS = 600


def vendor_path() -> Path:
    """Where `setup` installs Pillow for the running Python."""
    override = os.environ.get("ASYNX_VENDOR_PATH")
    if override:
        return Path(override).expanduser()
    version = f"py{sys.version_info.major}{sys.version_info.minor}"
    return state_path().expanduser().parent / "vendor" / version


def use_vendor_directory() -> None:
    directory = str(vendor_path())
    if os.path.isdir(directory) and directory not in sys.path:
        sys.path.insert(0, directory)


def pillow_version() -> str | None:
    """The importable Pillow's version, or None when it cannot be imported."""
    try:
        importlib.import_module("PIL.Image")
        return str(importlib.import_module("PIL").__version__)
    except (ImportError, AttributeError):
        return None


def missing_pillow() -> AsxError:
    return AsxError(
        "缺少图片处理依赖 Pillow，请先运行 setup 命令安装，然后重试。",
        code="pillow_not_installed",
        details={"next_action_args": ["setup"]},
    )


def _pip_command(target: Path) -> list[str]:
    return [
        sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
        "--upgrade", "--target", str(target), PILLOW_REQUIREMENT,
    ]


def _manual_command(target: Path) -> str:
    launcher = "py" if os.name == "nt" else "python3"
    return f'{launcher} -m pip install --upgrade --target "{target}" "{PILLOW_REQUIREMENT}"'


def _install_pillow(target: Path) -> None:
    manual = _manual_command(target)
    details = {"vendor_path": str(target), "manual_command": manual}
    if importlib.util.find_spec("pip") is None:
        raise AsxError(
            f"当前 Python 没有 pip，无法安装图片处理依赖 Pillow。请先为 {sys.executable} 安装 pip，"
            f"再重新运行 setup；也可在自己的终端手动安装：{manual}",
            code="pip_not_available",
            details=details,
        )
    try:
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        # pip's output may name a private index and its credentials: never relay it.
        returncode = subprocess.run(
            _pip_command(target), check=False, capture_output=True, timeout=_PIP_TIMEOUT_SECONDS
        ).returncode
    except (OSError, subprocess.SubprocessError):
        returncode = 1
    importlib.invalidate_caches()
    use_vendor_directory()
    if returncode or pillow_version() is None:
        raise AsxError(
            "图片处理依赖 Pillow 安装失败，请检查网络连接与目录写入权限后重新运行 setup；"
            f"也可在自己的终端手动安装：{manual}",
            code="dependency_install_failed",
            details=details,
        )


def setup() -> dict[str, Any]:
    """Make Pillow importable and say what is left for the user to do. Needs no key."""
    target = vendor_path()
    installed = False
    if pillow_version() is None:
        _install_pillow(target)
        installed = True
    status = config_status()
    ready = status["configured"] and status["key_valid"]
    command = configure_command()
    return {
        "ok": True,
        "skill_version": VERSION,
        "skill_path": str(Path(__file__).resolve().parents[2]),
        "python_executable": sys.executable,
        "pillow": {
            "available": True,
            "version": pillow_version(),
            "installed_now": installed,
            "vendor_path": str(target),
        },
        "configured": ready,
        "credential_source": status["credential_source"],
        "config_path": status["config_path"],
        "configure_command": command,
        "message": "图片处理依赖已就绪，API Key 已配置，可以开始使用。"
        if ready
        else f"图片处理依赖已就绪。尚未配置可用的 API Key，请在自己的终端运行以下命令输入密钥（请勿发送至对话）：{command}",
    }
