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
SKILL_SOURCE = ROOT / "skills" / "asynx-image"
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
        raise RuntimeError(f"无法加载已安装的 Skill，请重新运行安装命令。位置：{package}")
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
        raise RuntimeError(f"加载的 Skill 位置不正确，请重新运行安装命令。位置：{module_path}")
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
        raise RuntimeError("未检测到 Codex 或 Claude Code，请在安装命令后添加 --target codex、--target claude 或 --target both。")
    print("要安装到哪个 Agent？codex = Codex，claude = Claude Code，both = 两者", file=sys.stderr)
    while True:
        print("请选择（直接回车选择 Codex）：", end="", file=sys.stderr, flush=True)
        value = input().strip().casefold() or "codex"
        if value in {"codex", "claude"}:
            return [value]
        if value == "both":
            return ["codex", "claude"]
        print("请输入 codex、claude 或 both。", file=sys.stderr)


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
        return home / ".agents" / "skills" / "asynx-image"
    if target == "claude":
        return home / ".claude" / "skills" / "asynx-image"
    raise ValueError(f"Unknown target: {target}")


def _install_dependencies(destination: Path) -> None:
    vendor = destination / "scripts" / "vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    print("正在安装图片处理依赖，首次安装可能需要一些时间……", file=sys.stderr)
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
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError("图片处理依赖安装失败。请检查网络连接以及当前 Python 是否已安装 pip，然后重新运行安装命令。")


def _check_legacy_installations(home: Path, targets: list[str]) -> None:
    candidates = [_target_path(target, home).with_name("asx") for target in targets]
    if "codex" in targets:
        candidates.append(home / ".codex" / "skills" / "asx")
    existing = [path for path in candidates if path.exists() or path.is_symlink()]
    if existing:
        paths = "\n".join(str(path) for path in existing)
        raise RuntimeError(
            "检测到旧版 asx，需移除旧入口后继续安装 asynx-image：\n"
            f"{paths}\n"
            "可以让 Agent 帮你确认并将旧入口移出 skills 目录备份，再继续安装。\n"
            "请保留密钥、任务记录和生成图片，无需迁移这些数据，也不要仅将旧目录改名。"
        )


def _install_skill(destination: Path, *, install_dependencies: bool = True) -> None:
    if destination.exists() and not destination.is_dir():
        raise RuntimeError(f"安装位置被同名文件占用，请先检查该文件：{destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Keep the lock outside the replaced directory and ready for read-only CLI calls.
    # Never truncate or replace it: running commands may already hold this inode.
    with (destination.parent / ".asx-runtime.lock").open("ab"):
        pass
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
    client = _load_client_module(destination)
    update_files = importlib.import_module(f"{client.__name__}.update_files")
    update_files.record_install(destination, files=update_files.inventory(SKILL_SOURCE))


def _uninstall_skill(destination: Path) -> bool:
    if destination.is_symlink():
        destination.unlink()
        return True
    if not destination.exists():
        return False
    if not destination.is_dir() or destination.name != "asynx-image" or destination.parent.name != "skills":
        raise RuntimeError(f"该路径不是预期的 Skill 安装目录，未执行删除：{destination}")
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
        if exc.http_status == 401 or exc.code in {"missing_api_key", "invalid_api_key"}:
            message = "API Key 未通过验证，请检查密钥是否完整、有效，并在自己的终端重新配置。"
        elif exc.http_status == 403:
            message = "当前密钥无权访问模型目录，请检查密钥权限或联系服务管理员。"
        elif exc.code == "network_error":
            message = "暂时无法连接 Asynx，请检查网络或服务地址后重试；这不代表密钥错误。"
        elif exc.http_status == 429:
            message = "请求过于频繁，请稍后重试连接检查。"
        else:
            message = "暂时无法完成模型目录检查，请稍后重试；若持续失败，可将下方错误编号提供给支持人员。"
        print(f"Skill 已安装，但连接检查未通过。{message}", file=sys.stderr)
        print(f"错误编号：{exc.code}", file=sys.stderr)
        if exc.request_id:
            print(f"请求编号：{exc.request_id}", file=sys.stderr)
        print("无需重装 Skill，可回到 Agent 对话中请求检查配置和连接。", file=sys.stderr)
        return 1
    capable = [
        model
        for model in models
        if {"image.generate", "image.edit"}.intersection(model.get("task_types", []))
    ]
    if not capable:
        print("Skill 已安装，连接正常，但当前没有可用的图片模型。请稍后重试或联系服务管理员。", file=sys.stderr)
        return 1
    print(f"连接检查通过，可访问 {len(capable)} 个图片模型。本次未生成图片，实际生图权限和余额尚未验证。")
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
            print("没有找到已安装的 asynx-image skill。")
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
    _check_legacy_installations(user_home, targets)
    for target in targets:
        destination = _target_path(target, user_home)
        _install_skill(destination, install_dependencies=install_dependencies)
        label = "Codex" if target == "codex" else "Claude Code"
        print(f"已安装到 {label}：{destination}")

    client = _load_client_module(_target_path(targets[0], user_home))
    migrate = getattr(client, "migrate_legacy_config", None)
    if callable(migrate):
        migrated_path = migrate()
        if migrated_path is not None:
            print(f"已迁移旧配置，原文件已保留：{migrated_path}")
    print(f"配置文件：{client.config_path().resolve()}")
    if not args.skip_config:
        if _configured(client):
            if os.environ.get("ASYNX_API_KEY"):
                print("正在使用 ASYNX_API_KEY 环境变量中的密钥，它优先于配置文件；此步骤不会将它保存到配置文件。")
            else:
                print("正在使用已保存的 API Key，无需重复输入。")
        else:
            client.configure()
        if not args.no_verify:
            verification = _verify(client)
            if verification:
                return verification
        else:
            print("已跳过连接检查，尚未验证密钥是否可用。")
    else:
        print("已跳过密钥配置和连接检查；若尚未配置，请回到 Agent 对话中请求配置指引。")

    if "codex" in targets:
        print("回到 Codex，可输入：$asynx-image 生成一张卡通猫的图片")
    if "claude" in targets:
        print("回到 Claude Code，可输入：/asynx-image 生成一张卡通猫的图片")
    return 0


def main() -> int:
    try:
        return run()
    except (KeyboardInterrupt, EOFError):
        print("操作已中断。已完成的安装或配置步骤会保留，可以重新运行命令继续。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - installer failures share one CLI error path
        message = getattr(exc, "message", str(exc))
        print(f"未能完成操作：{message}", file=sys.stderr)
        return int(getattr(exc, "exit_code", 1))


if __name__ == "__main__":
    raise SystemExit(main())
