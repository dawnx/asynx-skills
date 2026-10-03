"""Keep installed skill files stable while a command is running."""

from __future__ import annotations

import errno
import importlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO


class LockBusyError(OSError):
    """Another process holds an incompatible lock; filesystem errors are separate."""


@contextmanager
def file_lock(path: Path, *, exclusive: bool = True) -> Iterator[None]:
    handle: BinaryIO
    if exclusive:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b")
    else:
        # Agent sandboxes can read installed skills without being allowed to write them.
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            # Older/manual installations may not have an installer-created lock yet.
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+b")
    with handle:
        if os.name == "nt":
            # LockFileEx supports shared readers; CRT byte locks serialize ordinary CLI commands.
            ctypes: Any = importlib.import_module("ctypes")
            wintypes: Any = importlib.import_module("ctypes.wintypes")
            msvcrt: Any = importlib.import_module("msvcrt")
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LockFileEx.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
            ]
            kernel.LockFileEx.restype = wintypes.BOOL
            kernel.UnlockFileEx.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
            ]
            kernel.UnlockFileEx.restype = wintypes.BOOL
            native_handle = msvcrt.get_osfhandle(handle.fileno())
            # Zeroed OVERLAPPED storage (32 bytes on Win64, 20 on Win32), offset zero.
            overlapped = ctypes.create_string_buffer(32)
            if not kernel.LockFileEx(
                native_handle, 1 | (2 if exclusive else 0), 0, 1, 0, overlapped
            ):
                error = ctypes.get_last_error()
                if error == 33:  # ERROR_LOCK_VIOLATION
                    raise LockBusyError(
                        errno.EAGAIN, "Skill lock is already held", str(path)
                    )
                raise ctypes.WinError(error)
            try:
                yield
            finally:
                kernel.UnlockFileEx(native_handle, 0, 1, 0, overlapped)
        else:
            lock_module: Any = importlib.import_module("fcntl")
            mode = lock_module.LOCK_EX if exclusive else lock_module.LOCK_SH
            try:
                lock_module.flock(handle, mode | lock_module.LOCK_NB)
            except OSError as exc:
                if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}:
                    raise LockBusyError(
                        errno.EAGAIN, "Skill lock is already held", str(path)
                    ) from exc
                raise
            try:
                yield
            finally:
                lock_module.flock(handle, lock_module.LOCK_UN)


def runtime_lock_path(root: Path) -> Path:
    return root.parent / ".asx-runtime.lock"
