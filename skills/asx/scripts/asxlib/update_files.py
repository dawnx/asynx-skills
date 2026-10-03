"""Installation receipts, verified packages and recoverable directory replacement."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import AsxError

REPOSITORY = "dawnx/asynx-skills"
RECEIPT = ".asx-install.json"
PILLOW_REQUIREMENT = "Pillow>=10.2,<13"
MAX_PACKAGE_BYTES = 16 * 1024 * 1024
MAX_UNPACKED_BYTES = 64 * 1024 * 1024
_VERSION = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def version_tuple(value: str) -> tuple[int, ...]:
    if not _VERSION.fullmatch(value):
        raise AsxError("更新版本号无效，仅支持稳定版本", code="invalid_update_version")
    return tuple(int(part) for part in value.split("."))


def read_version(root: Path) -> str | None:
    try:
        content = (root / "scripts" / "asxlib" / "constants.py").read_text(
            encoding="utf-8"
        )
    except (OSError, UnicodeError):
        return None
    match = re.search(r"^VERSION\s*=\s*[\"\']([^\"\']+)[\"\']", content, re.MULTILINE)
    return match.group(1) if match else None


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 512 * 1024 or path.is_symlink():
            raise ValueError("invalid metadata file")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TypeError("metadata must be an object")
        return data
    except (OSError, ValueError, TypeError) as exc:
        raise AsxError(
            f"无法读取更新记录：{path}", code="invalid_update_metadata"
        ) from exc


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".asx-", delete=False
        ) as handle:
            temporary = handle.name
            json.dump(data, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and Path(temporary).exists():
            Path(temporary).unlink()


def inventory(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (
            relative.parts[:2] == ("scripts", "vendor")
            or "__pycache__" in relative.parts
        ):
            continue
        if relative.as_posix() == RECEIPT or path.name == ".DS_Store":
            continue
        if path.is_symlink():
            raise AsxError(
                "安装目录包含链接，请通过原安装方式更新", code="unmanaged_installation"
            )
        if path.is_file():
            result[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def record_install(root: Path, *, files: dict[str, str] | None = None) -> None:
    version = read_version(root)
    if version is None:
        raise AsxError("安装包缺少版本信息", code="invalid_update_package")
    version_tuple(version)
    write_json(
        root / RECEIPT,
        {
            "schema": 1,
            "repository": REPOSITORY,
            "version": version,
            "files": inventory(root) if files is None else files,
        },
    )


def installation_problem(root: Path) -> str | None:
    if root.is_symlink() or root.absolute() != root.resolve():
        return "symlink"
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        return "source_checkout"
    try:
        receipt = read_json(root / RECEIPT)
        if receipt.get("schema") != 1 or receipt.get("repository") != REPOSITORY:
            return "untracked"
        if receipt.get("version") != read_version(root) or receipt.get(
            "files"
        ) != inventory(root):
            return "local_changes"
    except (AsxError, OSError):
        return "local_changes"
    return None


def extract_package(archive: Path, destination: Path, manifest: dict[str, Any]) -> Path:
    if archive.stat().st_size > MAX_PACKAGE_BYTES:
        raise AsxError("更新包超过大小限制", code="invalid_update_package")
    if hashlib.sha256(archive.read_bytes()).hexdigest() != manifest["sha256"]:
        raise AsxError(
            "更新包校验失败，现有安装未修改", code="update_checksum_mismatch"
        )
    try:
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if (
                len(entries) > 2000
                or sum(item.file_size for item in entries) > MAX_UNPACKED_BYTES
            ):
                raise ValueError("archive limits exceeded")
            seen: set[str] = set()
            for item in entries:
                parts = PurePosixPath(item.filename).parts
                mode = item.external_attr >> 16
                if (
                    not parts
                    or parts[0] != "asx"
                    or len(parts) < 2
                    or any(
                        part in {".", "..", ".git", "vendor", RECEIPT} for part in parts
                    )
                    or any("\\" in part or ":" in part for part in parts)
                    or any(
                        part.endswith((".", " "))
                        or part.split(".")[0].upper() in _WINDOWS_RESERVED
                        for part in parts
                    )
                    or item.filename.casefold() in seen
                    or stat.S_ISLNK(mode)
                    or (
                        stat.S_IFMT(mode)
                        and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))
                    )
                    or parts[1]
                    not in {
                        "SKILL.md",
                        "LICENSE",
                        "scripts",
                        "references",
                        "agents",
                        "assets",
                    }
                ):
                    raise ValueError("unsafe archive entry")
                seen.add(item.filename.casefold())
                target = destination.joinpath(*parts)
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with package.open(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError) as exc:
        raise AsxError(
            "更新包内容无效，现有安装未修改", code="invalid_update_package"
        ) from exc
    root = destination / "asx"
    if read_version(root) != manifest["version"]:
        raise AsxError("更新包版本与发布信息不一致", code="invalid_update_package")
    required = (
        root / "SKILL.md",
        root / "scripts" / "asynx.py",
        root / "scripts" / "asx_runtime.py",
    )
    if not all(path.is_file() for path in required):
        raise AsxError("更新包不完整", code="invalid_update_package")
    return root


def validate_manifest(value: Any, version: str) -> dict[str, Any]:
    version_tuple(version)
    if (
        not isinstance(value, dict)
        or value.get("schema") != 1
        or value.get("version") != version
        or value.get("archive") != f"asx-{version}.zip"
        or not isinstance(value.get("sha256"), str)
        or not _HASH.fullmatch(value["sha256"])
        or not isinstance(value.get("python_min"), str)
        or not re.fullmatch(r"3\.\d{1,2}", value["python_min"])
    ):
        raise AsxError("发布信息无效", code="invalid_update_manifest")
    return value


def prepare_dependencies(stage: Path, previous: Path) -> None:
    vendor = previous / "scripts" / "vendor"
    new_vendor = stage / "scripts" / "vendor"
    if vendor.is_dir() and not vendor.is_symlink():
        # Only the existing dependency directory is reused; skill code comes from the verified archive.
        shutil.copytree(vendor, new_vendor, symlinks=False)
    else:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--only-binary=:all:",
                "--target",
                str(new_vendor),
                PILLOW_REQUIREMENT,
            ],
            capture_output=True,
            check=False,
            timeout=180,
        )
        if result.returncode:
            raise AsxError(
                "图片依赖安装失败，现有安装未修改", code="update_dependencies_failed"
            )
    check = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            (
                "import sys; sys.path.insert(0, sys.argv[1]); import PIL; from PIL import Image, features; "
                "v=tuple(int(x) for x in PIL.__version__.split('.')[:2]); "
                "assert (10,2) <= v < (13,0); assert features.check('webp'); "
                "Image.new('RGB',(1,1)).close()"
            ),
            str(new_vendor),
        ],
        capture_output=True,
        check=False,
        timeout=30,
    )
    if check.returncode:
        raise AsxError(
            "图片依赖检查失败，请重新运行安装器", code="update_dependencies_failed"
        )


def smoke_test(stage: Path, version: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-c",
            (
                "import runpy,sys; sys.path.insert(0,sys.argv[1]); "
                "sys.argv=[sys.argv[1]+'/asynx.py','--version']; runpy.run_path(sys.argv[0],run_name='__main__')"
            ),
            str(stage / "scripts"),
        ],
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    if result.returncode or result.stdout.strip() != version:
        raise AsxError(
            "新版本启动检查失败，现有安装未修改", code="update_validation_failed"
        )


def _validated_entries(
    journal: dict[str, Any], allowed: list[Path]
) -> list[dict[str, Any]]:
    entries = journal.get("entries")
    if not isinstance(entries, list) or not entries:
        raise AsxError("更新恢复记录无效", code="invalid_update_metadata")
    known = {str(path) for path in allowed}
    used: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("target") not in known:
            raise AsxError("更新恢复目标不在安装列表中", code="invalid_update_metadata")
        target = Path(entry["target"])
        work = Path(str(entry.get("work", "")))
        if (
            str(target) in used
            or work.parent != target.parent
            or not work.name.startswith(".asx-update-")
            or work.is_symlink()
            or target.is_symlink()
            or target.absolute() != target.resolve()
        ):
            raise AsxError("更新恢复路径无效", code="invalid_update_metadata")
        used.add(str(target))
    return entries


def restore_transaction(
    journal_path: Path, allowed: list[Path], *, explicit: bool = False
) -> list[str]:
    journal = read_json(journal_path)
    if not journal or journal.get("status") not in {
        "committing",
        "committed",
        "rolling_back",
    }:
        return []
    if journal["status"] == "committed" and not explicit:
        return []
    entries = _validated_entries(journal, allowed)
    for entry in entries:
        backup = Path(entry["work"]) / "backup"
        if (
            backup.is_dir()
            and isinstance(entry.get("before"), dict)
            and inventory(backup) != entry["before"]
        ):
            raise AsxError(
                "旧版本备份已有改动，无法自动恢复", code="update_backup_changed"
            )
    if explicit:
        for entry in entries:
            target = Path(entry["target"])
            if installation_problem(target) is not None:
                raise AsxError(
                    "安装文件已有本地改动，无法直接回滚", code="local_changes"
                )
            if not (Path(entry["work"]) / "backup").is_dir():
                raise AsxError("旧版本备份已不存在", code="update_backup_missing")
    # Persist rollback intent too: a killed rollback must be resumable without requiring consumed backups.
    journal["status"] = "rolling_back"
    write_json(journal_path, journal)
    restored: list[str] = []
    for entry in reversed(entries):
        target = Path(entry["target"])
        work = Path(entry["work"])
        backup = work / "backup"
        if backup.exists():
            if backup.is_symlink() or (
                (work / "discarded").exists() and target.exists()
            ):
                raise AsxError("恢复目录需要人工检查", code="update_rollback_failed")
            if target.exists():
                target.rename(work / "discarded")
            backup.rename(target)
            restored.append(str(target))
    journal["status"] = "rolled_back"
    write_json(journal_path, journal)
    return restored


def replace_installations(
    package: Path,
    targets: list[Path],
    journal_path: Path,
    *,
    force: bool = False,
    dependencies: Callable[[Path, Path], None] | None = None,
) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    version = read_version(package)
    assert version is not None
    try:
        for target in targets:
            problem = installation_problem(target)
            if problem and (problem != "local_changes" or not force):
                raise AsxError(
                    "安装来源无法确认，请使用原安装器更新"
                    if problem != "local_changes"
                    else "安装文件有本地改动；保留改动，或使用 --force 备份后替换",
                    code=problem,
                    details={"path": str(target)},
                )
            before = inventory(target)
            work = Path(tempfile.mkdtemp(prefix=".asx-update-", dir=target.parent))
            entry: dict[str, Any] = {
                "target": str(target),
                "work": str(work),
                "before": before,
            }
            entries.append(entry)
            stage = work / "new"
            shutil.copytree(package, stage)
            (dependencies or prepare_dependencies)(stage, target)
            smoke_test(stage, version)
            record_install(stage)
        # An edit while downloads/dependencies were being prepared must not be overwritten.
        if any(inventory(Path(e["target"])) != e["before"] for e in entries):
            raise AsxError(
                "更新准备期间安装文件发生变化，请重新检查", code="local_changes"
            )
        journal: dict[str, Any] = {
            "schema": 1,
            "status": "committing",
            "version": version,
            "entries": entries,
        }
        write_json(journal_path, journal)
        try:
            for entry in entries:
                target = Path(entry["target"])
                work = Path(entry["work"])
                target.rename(work / "backup")
                (work / "new").rename(target)
            journal["status"] = "committed"
            write_json(journal_path, journal)
        except BaseException:
            try:
                restore_transaction(journal_path, targets)
            except (AsxError, OSError) as exc:
                raise AsxError(
                    "更新中断且自动恢复未完成；请运行 update rollback",
                    code="update_rollback_failed",
                    details={"journal_path": str(journal_path)},
                ) from exc
            raise
    finally:
        # Keep every backup/recovery directory; remove only never-committed staging copies.
        for entry in entries:
            work = Path(entry["work"])
            if (work / "new").exists() and not (work / "backup").exists():
                shutil.rmtree(work / "new")
    return {
        "ok": True,
        "updated": True,
        "version": version,
        "installations": [
            {"path": e["target"], "backup_path": str(Path(e["work"]) / "backup")}
            for e in entries
        ],
        "message": "升级完成。后续调用使用新版本；若 Agent 仍显示旧说明，请重新打开会话。",
    }


def lock_targets(
    stack: ExitStack, targets: list[Path], already_locked: Path | None
) -> None:
    from asx_runtime import file_lock, runtime_lock_path

    try:
        for target in sorted(set(targets)):
            if target != already_locked:
                stack.enter_context(file_lock(runtime_lock_path(target)))
    except OSError as exc:
        raise AsxError(
            "有命令正在使用 Skill，请在任务结束后更新", code="update_busy"
        ) from exc
