"""Directing a signal at one thread of another process is claimed only where Linux offers it."""

from __future__ import annotations

from pathlib import Path

from vs_sim.api.testing import TGKILL_SUPPORTED


def test_tgkill_is_claimed_only_where_its_procfs_thread_listing_exists() -> None:
    """The CPU name alone said yes on an Intel Mac, which numbers system calls differently."""
    assert not TGKILL_SUPPORTED or Path("/proc/self/task").is_dir()
