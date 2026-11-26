# SPDX-License-Identifier: GPL-2.0-only
"""One lock on the rig hardware for the UI and the CI runners.

The UI (station/ui/app.py) and the CI jobs (``python -m station.test_runner``,
``python -m station.perf_runner``) are separate processes that drive the same
HID, GPIO and camera. Both take an flock on ``var/rig.lock`` for the whole
operation. The UI asks without waiting and refuses when the lock is taken; a
CI job waits for it, so a UI action that is already running finishes first.
The kernel drops the lock when the holding process exits, also on a crash.
"""
import fcntl
import os
import sys
import time
from pathlib import Path

LOCK_PATH = Path(__file__).resolve().parent.parent / "var" / "rig.lock"


def _open(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(path, os.O_RDWR | os.O_CREAT, 0o644)


def _mark_holder(fd: int) -> None:
    """Write who holds the lock into the file, for holder() (best effort)."""
    try:
        name = os.path.basename(sys.argv[0]) if sys.argv and sys.argv[0] else "python"
        if name == "__main__.py" or name.endswith(".py"):
            name = " ".join(sys.argv[:3])
        os.ftruncate(fd, 0)
        os.pwrite(fd, f"{os.getpid()} {name}\n".encode(), 0)
    except OSError:
        pass


def holder(path: Path = LOCK_PATH) -> str:
    """"pid program" of the process that holds the lock, as it wrote it, or ""."""
    try:
        with open(path) as f:
            return f.readline().strip()[:120]
    except OSError:
        return ""


def try_acquire(path: Path = LOCK_PATH) -> int | None:
    """The lock's file descriptor, or None when another process holds it.
    Pass the descriptor to release()."""
    fd = _open(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    _mark_holder(fd)
    return fd


def acquire_waiting(log=print, path: Path = LOCK_PATH) -> int:
    """Wait until the lock is free and take it (the CI side). Logs once
    while it waits. The descriptor stays open until release() or exit."""
    fd = try_acquire(path)
    if fd is not None:
        return fd
    who = holder(path)
    log("[runner] the station UI is using the rig"
        + (f" ({who})" if who else "") + "; waiting for it to finish…")
    start = time.monotonic()
    fd = _open(path)
    fcntl.flock(fd, fcntl.LOCK_EX)
    _mark_holder(fd)
    log(f"[runner] rig free after {time.monotonic() - start:.0f} s")
    return fd


def release(fd: int | None) -> None:
    if fd is None:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
