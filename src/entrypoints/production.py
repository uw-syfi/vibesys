"""The real time sources and implementations a VibeSys process runs its runs with."""

from __future__ import annotations

from typing import TYPE_CHECKING

from entrypoints.launcher import source_checkout_root
from launch import LaunchSettings, default_run_readers, default_runs
from server.runtime import UrlWebHealthProbe, WebDiscovery
from vibesys.api.request import checkout_identity
from vs_project.api import Git
from vs_runtime.api.core import RunTiming
from vs_sim.api import (
    OsProcesses,
    OsProcessTable,
    OsThreads,
    PidfdProcessSignaller,
    SubprocessPipedProcessLauncher,
    SystemClock,
    ThreadBlockingRunner,
    UnixNetwork,
    UrllibLoopbackHttp,
)

if TYPE_CHECKING:
    from vibesys.api import RunReaders, Runs

__all__ = [
    "production_run_readers",
    "production_runs",
    "production_timing",
    "production_web_discovery",
]


def production_timing() -> RunTiming:
    """Wall time and real threads: the one definition of a real run's timing."""
    return RunTiming(SystemClock(), OsThreads())


def _production_settings() -> LaunchSettings:
    processes = OsProcesses()
    checkout = source_checkout_root()
    return LaunchSettings(
        checkout=("", False) if checkout is None else checkout_identity(checkout, Git(processes)),
        timing=production_timing(),
        processes=processes,
        piped_processes=SubprocessPipedProcessLauncher(),
        blocking=ThreadBlockingRunner(),
        network=UnixNetwork(),
        process_table=OsProcessTable(),
    )


def production_runs() -> Runs:
    """The in-process launcher over wall time, real threads, processes and sockets."""
    return default_runs(_production_settings())


def production_run_readers() -> RunReaders:
    """The opener of finished runs for conversation, over the same real implementations."""
    return default_run_readers(_production_settings())


def production_web_discovery() -> WebDiscovery:
    """Gateway discovery over this host's processes and its loopback health endpoint."""
    return WebDiscovery(PidfdProcessSignaller(), UrlWebHealthProbe(UrllibLoopbackHttp()))
