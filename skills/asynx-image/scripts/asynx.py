#!/usr/bin/env python3
"""Asynx 图片与视频 Agent Skill 的命令行入口。"""

import io
import sys

if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")

    import signal

    def _stop(_signal: int, _frame: object) -> None:
        # A host that stops the command still gets the JSON naming the task to resume.
        raise KeyboardInterrupt

    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop)

    from asxlib.cli import main
    from asxlib.dependencies import use_vendor_directory

    # Pillow installed by `setup` lives outside the Skill, which a channel may replace.
    use_vendor_directory()
    main()
