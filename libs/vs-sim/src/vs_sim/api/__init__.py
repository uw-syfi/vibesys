"""Public interfaces and real implementations of time, randomness, blocking calls, signals and processes.

Product code takes these interfaces; wiring passes the real implementations; tests pass
the simulators and Fakes from :mod:`vs_sim.api.testing`.

Names load on first use (PEP 562), so a process pays only for the modules it
touches: the host helper's ``hello`` path uses a few of these and must not load
the process, signal and streaming machinery behind the rest.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_sim.blocking import BlockingRunner, ThreadBlockingRunner
    from vs_sim.clock import (
        Clock,
        MonotonicClock,
        Sleeper,
        SleepingClock,
        SystemClock,
        utc_datetime,
    )
    from vs_sim.concurrency import Condition, Event, Lock, OsThreads, Threads, Worker
    from vs_sim.network import Connection, Listener, Network, UnixNetwork
    from vs_sim.probes import CommandProbe, ProbeResult, SubprocessProbe
    from vs_sim.processes import (
        CapturedTextProcess,
        CapturedTextProcessLauncher,
        ForegroundChild,
        ForegroundLauncher,
        InheritedStdioLauncher,
        MergedOutputProcess,
        MergedOutputProcessLauncher,
        PipedProcess,
        PipedProcessLauncher,
        PopenFactory,
        ProcessLauncher,
        ProcessOutcome,
        ProcessSpec,
        ProcessTimeoutError,
        RunningProcess,
        SubprocessCapturedTextProcessLauncher,
        SubprocessLauncher,
        SubprocessMergedOutputProcessLauncher,
        SubprocessPipedProcessLauncher,
        run_foreground,
    )
    from vs_sim.randomness import RandomSource, SeededRandom, SystemRandomSource, derive_seed
    from vs_sim.signals import (
        LoopSignalSource,
        PidfdProcessSignaller,
        ProcessSignaller,
        SignalSource,
    )
    from vs_sim.streaming import StreamingCommand, SubprocessStreamingCommand
    from vs_sim.sync import sync_descriptor
    from vs_sim.yields import yield_now

_MODULES: dict[str, str] = {
    "BlockingRunner": "vs_sim.blocking",
    "ThreadBlockingRunner": "vs_sim.blocking",
    "Clock": "vs_sim.clock",
    "MonotonicClock": "vs_sim.clock",
    "Sleeper": "vs_sim.clock",
    "SleepingClock": "vs_sim.clock",
    "SystemClock": "vs_sim.clock",
    "utc_datetime": "vs_sim.clock",
    "Condition": "vs_sim.concurrency",
    "Event": "vs_sim.concurrency",
    "Lock": "vs_sim.concurrency",
    "OsThreads": "vs_sim.concurrency",
    "Threads": "vs_sim.concurrency",
    "Worker": "vs_sim.concurrency",
    "Connection": "vs_sim.network",
    "Listener": "vs_sim.network",
    "Network": "vs_sim.network",
    "UnixNetwork": "vs_sim.network",
    "CommandProbe": "vs_sim.probes",
    "ProbeResult": "vs_sim.probes",
    "SubprocessProbe": "vs_sim.probes",
    "CapturedTextProcess": "vs_sim.processes",
    "CapturedTextProcessLauncher": "vs_sim.processes",
    "ForegroundChild": "vs_sim.processes",
    "ForegroundLauncher": "vs_sim.processes",
    "InheritedStdioLauncher": "vs_sim.processes",
    "MergedOutputProcess": "vs_sim.processes",
    "MergedOutputProcessLauncher": "vs_sim.processes",
    "PipedProcess": "vs_sim.processes",
    "PipedProcessLauncher": "vs_sim.processes",
    "PopenFactory": "vs_sim.processes",
    "ProcessLauncher": "vs_sim.processes",
    "ProcessOutcome": "vs_sim.processes",
    "ProcessSpec": "vs_sim.processes",
    "ProcessTimeoutError": "vs_sim.processes",
    "RunningProcess": "vs_sim.processes",
    "SubprocessCapturedTextProcessLauncher": "vs_sim.processes",
    "SubprocessLauncher": "vs_sim.processes",
    "SubprocessMergedOutputProcessLauncher": "vs_sim.processes",
    "SubprocessPipedProcessLauncher": "vs_sim.processes",
    "run_foreground": "vs_sim.processes",
    "RandomSource": "vs_sim.randomness",
    "SeededRandom": "vs_sim.randomness",
    "SystemRandomSource": "vs_sim.randomness",
    "derive_seed": "vs_sim.randomness",
    "LoopSignalSource": "vs_sim.signals",
    "PidfdProcessSignaller": "vs_sim.signals",
    "ProcessSignaller": "vs_sim.signals",
    "SignalSource": "vs_sim.signals",
    "StreamingCommand": "vs_sim.streaming",
    "SubprocessStreamingCommand": "vs_sim.streaming",
    "sync_descriptor": "vs_sim.sync",
    "yield_now": "vs_sim.yields",
}
"""The module that defines each public name."""


def __getattr__(name: str) -> object:
    """Import the module that defines ``name`` and cache the name here."""
    module = _MODULES.get(name)
    if module is None:
        message = f"module {__name__!r} has no attribute {name!r}"
        raise AttributeError(message)
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = [
    "BlockingRunner",
    "CapturedTextProcess",
    "CapturedTextProcessLauncher",
    "Clock",
    "CommandProbe",
    "Condition",
    "Connection",
    "Event",
    "ForegroundChild",
    "ForegroundLauncher",
    "InheritedStdioLauncher",
    "Listener",
    "Lock",
    "LoopSignalSource",
    "MergedOutputProcess",
    "MergedOutputProcessLauncher",
    "MonotonicClock",
    "Network",
    "OsThreads",
    "PidfdProcessSignaller",
    "PipedProcess",
    "PipedProcessLauncher",
    "PopenFactory",
    "ProbeResult",
    "ProcessLauncher",
    "ProcessOutcome",
    "ProcessSignaller",
    "ProcessSpec",
    "ProcessTimeoutError",
    "RandomSource",
    "RunningProcess",
    "SeededRandom",
    "SignalSource",
    "Sleeper",
    "SleepingClock",
    "StreamingCommand",
    "SubprocessCapturedTextProcessLauncher",
    "SubprocessLauncher",
    "SubprocessMergedOutputProcessLauncher",
    "SubprocessPipedProcessLauncher",
    "SubprocessProbe",
    "SubprocessStreamingCommand",
    "SystemClock",
    "SystemRandomSource",
    "ThreadBlockingRunner",
    "Threads",
    "UnixNetwork",
    "Worker",
    "derive_seed",
    "run_foreground",
    "sync_descriptor",
    "utc_datetime",
    "yield_now",
]
