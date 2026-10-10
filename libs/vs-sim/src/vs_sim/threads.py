"""Real signal delivery to one chosen thread of another process (Linux)."""

from __future__ import annotations

import ctypes
import platform
from pathlib import Path

_TGKILL_SYSCALL = {"x86_64": 234, "aarch64": 131}

TGKILL_SUPPORTED = platform.machine() in _TGKILL_SYSCALL
"""Whether this machine's ``tgkill`` syscall number is known."""


def non_main_thread_ids(pid: int) -> list[int]:
    """Return the ids of every thread of *pid* other than its main thread."""
    return sorted(
        int(entry.name) for entry in Path(f"/proc/{pid}/task").iterdir() if int(entry.name) != pid
    )


def send_to_thread(pid: int, thread_id: int, number: int) -> None:
    """Deliver signal *number* to thread *thread_id* of *pid*, as the kernel could for a process signal."""
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(_TGKILL_SYSCALL[platform.machine()], pid, thread_id, number)
    if result != 0:
        raise OSError(ctypes.get_errno(), "tgkill failed")
