"""A run's time sources for tests that run the product on the real clock."""

from __future__ import annotations

from vs_runtime.api.core import PRODUCTION_LEASE_SECONDS, RunTiming
from vs_sim.api import OsThreads, SystemClock


def real_timing() -> RunTiming:
    """Wall time, real threads and the production lease, as a production entrypoint builds them."""
    return RunTiming(SystemClock(), PRODUCTION_LEASE_SECONDS, OsThreads())
