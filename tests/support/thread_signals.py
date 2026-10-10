"""Real signal delivery to one chosen thread of a child process (Linux)."""

from __future__ import annotations

import pytest

from vs_sim.api.testing import TGKILL_SUPPORTED, non_main_thread_ids, send_to_thread

requires_tgkill = pytest.mark.skipif(
    not TGKILL_SUPPORTED, reason="tgkill syscall number is unknown"
)

__all__ = ["non_main_thread_ids", "requires_tgkill", "send_to_thread"]
