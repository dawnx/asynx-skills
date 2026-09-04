#!/usr/bin/env python3
"""Asynx 图片 Agent Skill 的稳定命令行入口。"""

import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent / "vendor"
if _VENDOR.is_dir():
    sys.path.insert(0, str(_VENDOR))

from asxlib.cli import main

if __name__ == "__main__":
    main()
