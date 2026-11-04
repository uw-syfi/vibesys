"""Real signal delivery to one chosen thread of another process (Linux)."""

from __future__ import annotations

import ctypes
import platform
import sys
from pathlib import Path

# Linux's system call numbers, per CPU architecture. The numbers are Linux's ABI: the same
# CPU on macOS (x86_64) numbers its system calls differently and has no ``/proc``.
_LINUX_TGKILL_SYSCALL = {"x86_64": 234, "aarch64": 131}

TGKILL_SUPPORTED = sys.platform.startswith("linux") and platform.machine() in _LINUX_TGKILL_SYSCALL
"""Whether this is Linux on a machine whose ``tgkill`` system call number is known."""


def non_main_thread_ids(pid: int) -> list[int]:
    """Return the ids of every thread of *pid* other than its main thread."""
    return sorted(
        int(entry.name) for entry in Path(f"/proc/{pid}/task").iterdir() if int(entry.name) != pid
    )


def send_to_thread(pid: int, thread_id: int, number: int) -> None:
    """Deliver signal *number* to thread *thread_id* of *pid*, as the kernel could for a process signal."""
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.syscall(_LINUX_TGKILL_SYSCALL[platform.machine()], pid, thread_id, number)
    if result != 0:
        raise OSError(ctypes.get_errno(), "tgkill failed")
