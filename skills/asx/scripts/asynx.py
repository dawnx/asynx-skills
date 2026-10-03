#!/usr/bin/env python3
"""Asynx 图片 Agent Skill 的稳定命令行入口。"""

import io
import json
import sys
from contextlib import nullcontext
from pathlib import Path

from asx_runtime import LockBusyError, file_lock, runtime_lock_path

_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir():
    sys.path.insert(0, str(_VENDOR))

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    managed = (root / ".asx-install.json").is_file() or (
        root.name == "asx"
        and root.parent.name == "skills"
        and root.parent.parent.name in {".agents", ".claude", ".codex"}
    )
    changing = sys.argv[1:3] in (["update", "apply"], ["update", "rollback"])
    guard = (
        file_lock(runtime_lock_path(root), exclusive=changing)
        if managed
        else nullcontext()
    )
    try:
        guard.__enter__()
    except OSError as exc:
        busy = isinstance(exc, LockBusyError)
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "code": "update_busy" if busy else "runtime_lock_error",
                        "message": "Skill 正在使用或更新中，请稍后重试"
                        if busy
                        else f"无法访问 Skill 运行锁，请检查目录权限或重新运行安装器：{exc}",
                        "details": {"path": str(runtime_lock_path(root)), "errno": exc.errno},
                    },
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(2) from None
    try:
        from asxlib.cli import main

        main(locked_root=root if managed else None)
    finally:
        guard.__exit__(None, None, None)
