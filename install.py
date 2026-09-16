#!/usr/bin/env python3
"""Install the Asynx image skill and configure its API key."""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent
SKILL_SOURCE = ROOT / "skills" / "asx"
PILLOW_REQUIREMENT = "Pillow>=10.2,<13"


def _load_client_module(skill_root: Path) -> ModuleType:
    package = (skill_root / "scripts" / "asxlib").resolve()
    module_name = "_asynx_installed"
    for name in tuple(sys.modules):
        if name == module_name or name.startswith(f"{module_name}."):
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        module_name,
        package / "__init__.py",
        submodule_search_locations=[str(package)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load the installed Asynx client from: {package}")
    client = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = client
    try:
        spec.loader.exec_module(client)
    except Exception:
        for name in tuple(sys.modules):
            if name == module_name or name.startswith(f"{module_name}."):
                del sys.modules[name]
        raise
    module_path = Path(client.__file__ or "").resolve()
    if not module_path.is_relative_to(package):
        raise RuntimeError(f"Loaded Asynx client from an unexpected path: {module_path}")
    return client


def _detected_targets(home: Path) -> list[str]:
    detected: list[str] = []
    if (home / ".codex").exists() or (home / ".agents").exists():
        detected.append("codex")
    if (home / ".claude").exists():
        detected.append("claude")
    return detected


def _prompt_target() -> list[str]:
    if not sys.stdin.isatty():
        raise RuntimeError("No agent installation was detected; pass --target codex, claude, or both")
    print("Install for Codex, Claude Code, or both? [codex/claude/both]", file=sys.stderr)
    while True:
        print("Target [codex]: ", end="", file=sys.stderr, flush=True)
        value = input().strip().casefold() or "codex"
        if value in {"codex", "claude"}:
            return [value]
        if value == "both":
            return ["codex", "claude"]
        print("Enter codex, claude, or both.", file=sys.stderr)


def _resolve_targets(selection: str, home: Path) -> list[str]:
    if selection == "codex":
        return ["codex"]
    if selection == "claude":
        return ["claude"]
    if selection == "both":
        return ["codex", "claude"]
    detected = _detected_targets(home)
    return detected or _prompt_target()


def _target_path(target: str, home: Path) -> Path:
    if target == "codex":
        return home / ".agents" / "skills" / "asx"
    if target == "claude":
        return home / ".claude" / "skills" / "asx"
    raise ValueError(f"Unknown target: {target}")


def _install_dependencies(destination: Path) -> None:
    vendor = destination / "scripts" / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--upgrade",
            "--target",
            str(vendor),
            PILLOW_REQUIREMENT,
        ],
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"无法将 Pillow 安装到 skill 的隔离依赖目录：{vendor}")


def _install_skill(destination: Path, *, install_dependencies: bool = True) -> None:
    if destination.exists() and not destination.is_dir():
        raise RuntimeError(f"Installation target is not a directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        SKILL_SOURCE,
        destination,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "vendor"),
    )
    script = destination / "scripts" / "asynx.py"
    script.chmod(script.stat().st_mode | 0o111)
    if install_dependencies:
        _install_dependencies(destination)


def _uninstall_skill(destination: Path) -> bool:
    if destination.is_symlink():
        destination.unlink()
        return True
    if not destination.exists():
        return False
    if not destination.is_dir() or destination.name != "asx" or destination.parent.name != "skills":
        raise RuntimeError(f"Refusing to remove an unexpected path: {destination}")
    shutil.rmtree(destination)
    return True


def _installed_targets(home: Path) -> list[str]:
    return [target for target in ("codex", "claude") if _target_path(target, home).exists()]


def _configured(client: ModuleType) -> bool:
    key = os.environ.get("ASYNX_API_KEY")
    if isinstance(key, str) and key.strip():
        return True
    config = client.read_config()
    configured = config.get("api_key")
    if not isinstance(configured, str) or not configured.strip():
        return False
    try:
        client.validate_api_key(configured)
    except client.AsxError:
        return False
    return True


def _verify(client: ModuleType) -> int:
    try:
        api_key, base_url = client.load_credentials()
        models, _request_id = client.AsynxClient(base_url, api_key).models()
    except client.AsxError as exc:
        print(f"Configuration check failed: {exc.message}", file=sys.stderr)
        print("The skill is installed. Fix the key or network connection and run install.py again.", file=sys.stderr)
        return 1
    capable = [
        model
        for model in models
        if {"image.generate", "image.edit"}.intersection(model.get("task_types", []))
    ]
    if not capable:
        print("Configuration check failed: no image models are currently available.", file=sys.stderr)
        return 1
    print(f"Configuration verified: {len(capable)} image model(s) available.")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="安装、更新或卸载 Asynx 图片 Agent Skill")
    parser.add_argument(
        "--target",
        choices=("auto", "codex", "claude", "both"),
        default="auto",
        help="安装目标，默认自动检测 Codex/Claude Code",
    )
    parser.add_argument(
        "--skip-config",
        action="store_true",
        help="安装但不询问 API Key",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过只读 API Key 和模型目录检查",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="卸载已安装的 skill，不删除 API Key 和批次数据",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="卸载时跳过确认，仅与 --uninstall 一起使用",
    )
    return parser


def run(
    argv: list[str] | None = None,
    *,
    home: Path | None = None,
    install_dependencies: bool = True,
) -> int:
    args = _parser().parse_args(argv)
    user_home = (home or Path.home()).resolve()
    if args.uninstall:
        targets = _installed_targets(user_home) if args.target == "auto" else _resolve_targets(args.target, user_home)
        if not targets:
            print("没有找到已安装的 asx skill。")
            return 0
        if not args.yes:
            if not sys.stdin.isatty():
                raise RuntimeError("非交互卸载需要加 --yes")
            print(f"将卸载：{', '.join(targets)}。API Key、批次数据库和生成图片不会删除。", file=sys.stderr)
            print("确认卸载？[y/N] ", end="", file=sys.stderr, flush=True)
            if input().strip().casefold() not in {"y", "yes"}:
                print("已取消卸载。", file=sys.stderr)
                return 0
        for target in targets:
            destination = _target_path(target, user_home)
            if _uninstall_skill(destination):
                label = "Codex" if target == "codex" else "Claude Code"
                print(f"已从 {label} 卸载：{destination}")
        return 0
    targets = _resolve_targets(args.target, user_home)
    for target in targets:
        destination = _target_path(target, user_home)
        _install_skill(destination, install_dependencies=install_dependencies)
        label = "Codex" if target == "codex" else "Claude Code"
        print(f"Installed for {label}: {destination}")

    client = _load_client_module(_target_path(targets[0], user_home))
    migrate = getattr(client, "migrate_legacy_config", None)
    if callable(migrate):
        migrated_path = migrate()
        if migrated_path is not None:
            print(f"Migrated legacy configuration: {migrated_path}")
    print(f"Configuration file: {client.config_path().resolve()}")
    if not args.skip_config:
        if _configured(client):
            print("Using the existing Asynx API key configuration.")
        else:
            client.configure()
            print(f"Saved configuration: {client.config_path()}")
        if not args.no_verify:
            verification = _verify(client)
            if verification:
                return verification

    if "codex" in targets:
        print("Try in Codex: $asx generate an image of a red chair")
    if "claude" in targets:
        print("Try in Claude Code: /asx generate an image of a red chair")
    return 0


def main() -> int:
    try:
        return run()
    except Exception as exc:  # noqa: BLE001 - installer failures share one CLI error path
        message = getattr(exc, "message", str(exc))
        print(f"Installation failed: {message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
