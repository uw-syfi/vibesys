"""The real time sources and implementations a VibeSys process runs its runs with."""

from __future__ import annotations

from typing import TYPE_CHECKING

from launch import LaunchSettings, default_runs
from server.runtime import UrlWebHealthProbe, WebDiscovery
from vs_runtime.api.core import PRODUCTION_LEASE_SECONDS, RunTiming
from vs_sim.api import (
    OsProcesses,
    OsThreads,
    PidfdProcessSignaller,
    SystemClock,
    ThreadBlockingRunner,
    UnixNetwork,
    UrllibLoopbackHttp,
)

if TYPE_CHECKING:
    from vibesys.api import Runs

__all__ = ["production_runs", "production_web_discovery"]


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


def production_web_discovery() -> WebDiscovery:
    """Gateway discovery over this host's processes and its loopback health endpoint."""
    return WebDiscovery(PidfdProcessSignaller(), UrlWebHealthProbe(UrllibLoopbackHttp()))
