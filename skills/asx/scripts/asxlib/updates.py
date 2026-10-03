"""Stable release discovery and explicit updates; never uses Asynx credentials."""

from __future__ import annotations

import json
import os
import shlex
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from asx_runtime import file_lock

from .config import cache_path, config_path, state_path
from .constants import VERSION
from .errors import AsxError
from .update_files import (
    MAX_PACKAGE_BYTES,
    RECEIPT,
    REPOSITORY,
    extract_package,
    installation_problem,
    lock_targets,
    read_json,
    read_version,
    replace_installations,
    restore_transaction,
    validate_manifest,
    version_tuple,
    write_json,
)

CHECK_INTERVAL_SECONDS = 6 * 60 * 60
LATEST_RELEASE_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_BASE_URL = f"https://github.com/{REPOSITORY}/releases"
_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "release-assets.githubusercontent.com",
    "objects.githubusercontent.com",
}


def skill_root() -> Path:
    return Path(__file__).resolve().parents[2]


def update_cache_path() -> Path:
    return cache_path().with_name("updates.json")


def journal_path() -> Path:
    return update_cache_path().with_name("update-transaction.json")


def known_installations() -> dict[str, Path]:
    user_home = Path.home().resolve()
    return {
        "codex": user_home / ".agents" / "skills" / "asx",
        "claude": user_home / ".claude" / "skills" / "asx",
        "codex-legacy": user_home / ".codex" / "skills" / "asx",
    }


def _candidates() -> dict[str, Path]:
    candidates = known_installations()
    current = skill_root()
    if current not in candidates.values():
        candidates["current"] = current
    return candidates


def update_command() -> str:
    script = skill_root() / "scripts" / "asynx.py"
    if os.name == "nt":
        return f'py "{script}" update apply'
    return f"python3 {shlex.quote(str(script))} update apply"


def _cache() -> dict[str, Any]:
    try:
        return read_json(update_cache_path())
    except AsxError:
        return {}


def _cached_release(cache: dict[str, Any]) -> dict[str, str] | None:
    release = cache.get("release")
    if not isinstance(release, dict) or not isinstance(release.get("version"), str):
        return None
    try:
        return _release_descriptor(release["version"])
    except AsxError:
        return None


def _newer(latest: str, installed: str | None) -> bool:
    try:
        return installed is not None and version_tuple(latest) > version_tuple(
            installed
        )
    except AsxError:
        return False


def update_status() -> dict[str, Any]:
    cache = _cache()
    release = _cached_release(cache)
    latest = release["version"] if release else None
    installations = []
    for agent, root in _candidates().items():
        if not root.is_dir():
            continue
        version = read_version(root)
        problem = installation_problem(root)
        installations.append(
            {
                "agent": agent,
                "path": str(root),
                "version": version,
                "current": root == skill_root(),
                "managed": problem in {None, "local_changes"},
                "update_blocked_by": problem,
                "update_available": _newer(latest, version) if latest else None,
            }
        )
    journal = read_json(journal_path())
    checked_at = cache.get("checked_at")
    fresh = (
        isinstance(checked_at, (float, int))
        and 0 <= time.time() - checked_at < CHECK_INTERVAL_SECONDS
    )
    return {
        "ok": True,
        "current_version": VERSION,
        "latest_version": latest,
        "update_available": (
            _newer(latest, VERSION) or any(i["update_available"] for i in installations)
        )
        if latest
        else None,
        "check_status": cache.get("check_status", "not_checked"),
        "checked_at": checked_at,
        "cache_stale": not fresh,
        "check_interval_hours": 6,
        "automatic_check_enabled": os.environ.get("ASYNX_UPDATE_CHECK", "").lower()
        != "off",
        "installed_copies": installations,
        "recovery_required": journal.get("status") in {"committing", "rolling_back"},
        "rollback_available": journal.get("status") == "committed",
        "release_url": release["release_url"] if release else None,
        "update_command": update_command(),
    }


def _validate_download_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname not in _DOWNLOAD_HOSTS
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.port not in {None, 443}
    ):
        raise AsxError("更新下载地址不受支持", code="invalid_update_url")


class _ReleaseRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Request | None:
        _validate_download_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch(url: str, *, limit: int, timeout: float) -> bytes:
    _validate_download_url(url)
    request = Request(
        url,
        headers={
            "User-Agent": f"asx-skill/{VERSION}",
            "Accept": "application/json, application/octet-stream",
        },
    )
    started = time.monotonic()
    try:
        with build_opener(_ReleaseRedirect()).open(
            request, timeout=timeout
        ) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > limit:
                raise AsxError("更新响应超过大小限制", code="update_response_too_large")
            chunks = bytearray()
            while True:
                chunk = response.read(min(64 * 1024, limit + 1 - len(chunks)))
                if not chunk:
                    return bytes(chunks)
                chunks.extend(chunk)
                if len(chunks) > limit:
                    raise AsxError(
                        "更新响应超过大小限制", code="update_response_too_large"
                    )
                if time.monotonic() - started > timeout:
                    raise TimeoutError("update download timed out")
    except HTTPError as exc:
        status = exc.code
        exc.close()
        code = "no_stable_release" if status == 404 else "update_check_unavailable"
        raise AsxError(
            "尚无稳定发布版" if status == 404 else "暂时无法检查更新", code=code
        ) from exc


def _release_descriptor(version: str) -> dict[str, str]:
    version_tuple(version)
    base = f"{RELEASE_BASE_URL}/download/v{version}"
    return {
        "version": version,
        "release_url": f"{RELEASE_BASE_URL}/tag/v{version}",
        "manifest_url": f"{base}/asx-{version}.json",
        "archive_url": f"{base}/asx-{version}.zip",
    }


def _latest_release(*, timeout: float) -> dict[str, str]:
    data = json.loads(_fetch(LATEST_RELEASE_URL, limit=256 * 1024, timeout=timeout))
    if not isinstance(data, dict) or data.get("draft") or data.get("prerelease"):
        raise AsxError("未找到有效的稳定发布版", code="invalid_update_manifest")
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v"):
        raise AsxError("发布版本号无效", code="invalid_update_version")
    descriptor = _release_descriptor(tag[1:])
    assets = data.get("assets")
    if not isinstance(assets, list):
        raise AsxError("发布文件不完整", code="invalid_update_manifest")
    names = {
        a.get("name")
        for a in assets
        if isinstance(a, dict) and isinstance(a.get("name"), str)
    }
    if not {f"asx-{tag[1:]}.zip", f"asx-{tag[1:]}.json"} <= names:
        raise AsxError("发布文件不完整", code="invalid_update_manifest")
    return descriptor


def update_check(*, force: bool = False, automatic: bool = False) -> dict[str, Any]:
    path = update_cache_path()
    try:
        with file_lock(path.with_suffix(".lock")):
            cache = _cache()
            checked_at = cache.get("checked_at")
            fresh = (
                isinstance(checked_at, (int, float))
                and 0 <= time.time() - checked_at < CHECK_INTERVAL_SECONDS
            )
            if not fresh or force:
                try:
                    release = _latest_release(timeout=2.0 if automatic else 10.0)
                    cache = {
                        "checked_at": time.time(),
                        "check_status": "available",
                        "release": release,
                    }
                except (AsxError, OSError, ValueError) as exc:
                    cache = {
                        "checked_at": time.time(),
                        "check_status": "unavailable",
                        "error_code": exc.code
                        if isinstance(exc, AsxError)
                        else "update_check_unavailable",
                    }
                write_json(path, cache)
        result = update_status()
        result["ok"] = result["check_status"] == "available"
        result["cached"] = fresh and not force
        if not result["ok"]:
            result["error"] = {
                "code": cache.get("error_code", "update_check_unavailable"),
                "message": "目前暂无稳定发布版，仍可使用当前版本。"
                if cache.get("error_code") == "no_stable_release"
                else "暂时无法确认是否有更新，仍可使用当前版本。",
            }
        return result
    except OSError as exc:
        raise AsxError(
            "更新检查正在进行或缓存不可写", code="update_check_unavailable"
        ) from exc


def automatic_update_notice() -> dict[str, Any] | None:
    if (
        os.environ.get("ASYNX_UPDATE_CHECK", "").lower() == "off"
        or not (skill_root() / RECEIPT).is_file()
    ):
        return None
    try:
        result = update_check(automatic=True)
        if result["ok"] and result["update_available"]:
            return {
                "current_version": VERSION,
                "latest_version": result["latest_version"],
                "message": f"asx {result['latest_version']} 已发布，可运行 update apply 升级。",
                "update_command": result["update_command"],
                "release_url": result["release_url"],
                "outdated_copies": [
                    i for i in result["installed_copies"] if i["update_available"]
                ],
            }
    except Exception:  # noqa: BLE001 - optional update notices must never discard a completed image result
        return None
    return None


def _selected_targets(selection: str) -> list[Path]:
    candidates = _candidates()
    if selection == "current":
        return [skill_root()]
    if selection == "all":
        return [
            p
            for p in dict.fromkeys(candidates.values())
            if p.is_dir() and installation_problem(p) != "source_checkout"
        ]
    names = ("codex", "codex-legacy") if selection == "codex" else (selection,)
    return [candidates[n] for n in names if n in candidates and candidates[n].is_dir()]


def _allowed_targets() -> list[Path]:
    return [
        p
        for p in dict.fromkeys(_candidates().values())
        if installation_problem(p) != "source_checkout"
    ]


def update_apply(
    *,
    target: str = "all",
    force: bool = False,
    already_locked: Path | None = None,
) -> dict[str, Any]:
    targets = _selected_targets(target)
    allowed = _allowed_targets()
    try:
        with ExitStack() as stack:
            stack.enter_context(file_lock(journal_path().with_suffix(".lock")))
            # Recover a killed update before preparing another one. No Asynx state is opened.
            journal = read_json(journal_path())
            recovery = journal.get("status") in {"committing", "rolling_back"}
            locked = list(dict.fromkeys([*targets, *allowed])) if recovery else targets
            lock_targets(stack, locked, already_locked)
            restored = restore_transaction(journal_path(), allowed)
            if restored:
                return {
                    "ok": True,
                    "updated": False,
                    "recovered": restored,
                    "message": "已恢复上次中断的安装，请重新运行 update apply。",
                }
            if not targets:
                raise AsxError(
                    "未找到可更新的已安装副本，请先运行安装器",
                    code="no_installed_skills",
                )
            for root in targets:
                if any(
                    p.resolve().is_relative_to(root.resolve())
                    for p in (config_path(), state_path(), cache_path())
                ):
                    raise AsxError(
                        "配置或任务数据位于 Skill 安装目录内，请先移到用户数据目录",
                        code="update_data_inside_installation",
                    )
                problem = installation_problem(root)
                if problem and (not force or problem != "local_changes"):
                    raise AsxError(
                        "检测到本地改动，请备份后使用 --force 更新"
                        if problem == "local_changes"
                        else "此副本未由当前安装器管理，请使用原安装器更新一次",
                        code=problem,
                        details={"path": str(root)},
                    )
            release = _latest_release(timeout=10.0)
            version = release["version"]
            targets = [p for p in targets if _newer(version, read_version(p))]
            if not targets:
                return {
                    "ok": True,
                    "updated": False,
                    "latest_version": version,
                    "message": "已是最新稳定版。",
                }
            manifest = validate_manifest(
                json.loads(
                    _fetch(release["manifest_url"], limit=256 * 1024, timeout=15.0)
                ),
                version,
            )
            minimum = tuple(int(x) for x in manifest["python_min"].split("."))
            if sys.version_info[:2] < minimum:
                raise AsxError(
                    f"新版本需要 Python {manifest['python_min']} 或更高版本",
                    code="unsupported_update_python",
                )
            with tempfile.TemporaryDirectory(prefix="asx-release-") as directory:
                temporary = Path(directory)
                archive = temporary / manifest["archive"]
                archive.write_bytes(
                    _fetch(
                        release["archive_url"], limit=MAX_PACKAGE_BYTES, timeout=60.0
                    )
                )
                package = extract_package(archive, temporary / "unpacked", manifest)
                result = replace_installations(
                    package, targets, journal_path(), force=force
                )
            return result
    except (OSError, ValueError) as exc:
        raise AsxError(
            "升级未完成，现有文件已保留或恢复；请稍后重试", code="update_failed"
        ) from exc


def update_rollback(*, already_locked: Path | None = None) -> dict[str, Any]:
    allowed = _allowed_targets()
    try:
        with ExitStack() as stack:
            stack.enter_context(file_lock(journal_path().with_suffix(".lock")))
            lock_targets(stack, allowed, already_locked)
            journal = read_json(journal_path())
            restored = restore_transaction(
                journal_path(), allowed, explicit=journal.get("status") == "committed"
            )
            return {
                "ok": True,
                "restored": restored,
                "message": "已恢复上一个版本。" if restored else "没有需要恢复的更新。",
            }
    except OSError as exc:
        raise AsxError(
            "无法恢复更新，请检查安装目录权限和更新记录", code="update_rollback_failed"
        ) from exc
