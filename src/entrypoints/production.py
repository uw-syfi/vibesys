"""The real time sources and implementations a VibeSys process runs its runs with."""

from __future__ import annotations

from typing import TYPE_CHECKING

from launch import LaunchSettings, default_runs
from vs_runtime.api.core import PRODUCTION_LEASE_SECONDS, RunTiming
from vs_sim.api import OsProcesses, OsThreads, SystemClock, ThreadBlockingRunner, UnixNetwork

if TYPE_CHECKING:
    from vibesys.api import Runs

__all__ = ["production_runs"]


def production_runs() -> Runs:
    """The in-process launcher over wall time, real threads, processes, sockets and the production lease."""
    timing = RunTiming(SystemClock(), PRODUCTION_LEASE_SECONDS, OsThreads())
    return default_runs(
        LaunchSettings(
            timing=timing,
            processes=OsProcesses(),
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
        )
    )
