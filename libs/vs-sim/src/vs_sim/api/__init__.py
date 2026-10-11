"""Public interfaces and real implementations of time, randomness, blocking calls, signals and processes.

Product code takes these interfaces; wiring passes the real implementations; tests pass
the simulators and Fakes from :mod:`vs_sim.api.testing`.
"""

from vs_sim.blocking import BlockingRunner, ThreadBlockingRunner
from vs_sim.clock import Clock, MonotonicClock, Sleeper, SleepingClock, SystemClock
from vs_sim.concurrency import Condition, Event, Lock, OsThreads, Threads, Worker
from vs_sim.network import Connection, Listener, Network, UnixNetwork
from vs_sim.probes import CommandProbe, ProbeResult, SubprocessProbe
from vs_sim.processes import (
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
    SubprocessLauncher,
    SubprocessMergedOutputProcessLauncher,
    SubprocessPipedProcessLauncher,
    run_foreground,
)
from vs_sim.randomness import RandomSource, SeededRandom, SystemRandomSource, derive_seed
from vs_sim.signals import LoopSignalSource, PidfdProcessSignaller, ProcessSignaller, SignalSource
from vs_sim.streaming import StreamingCommand, SubprocessStreamingCommand

__all__ = [
    "BlockingRunner",
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
]
