"""Public interfaces and real implementations of time, randomness, blocking calls, signals and processes.

Product code takes these interfaces; wiring passes the real implementations; tests pass
the simulators and Fakes from :mod:`vs_sim.api.testing`.
"""

from vs_sim.blocking import BlockingRunner, ThreadBlockingRunner
from vs_sim.clock import Clock, MonotonicClock, Sleeper, SleepingClock, SystemClock
from vs_sim.processes import (
    ProcessLauncher,
    ProcessOutcome,
    ProcessSpec,
    RunningProcess,
    SubprocessLauncher,
)
from vs_sim.randomness import RandomSource, SeededRandom, SystemRandomSource, derive_seed
from vs_sim.signals import LoopSignalSource, SignalSource

__all__ = [
    "BlockingRunner",
    "Clock",
    "LoopSignalSource",
    "MonotonicClock",
    "ProcessLauncher",
    "ProcessOutcome",
    "ProcessSpec",
    "RandomSource",
    "RunningProcess",
    "SeededRandom",
    "SignalSource",
    "Sleeper",
    "SleepingClock",
    "SubprocessLauncher",
    "SystemClock",
    "SystemRandomSource",
    "ThreadBlockingRunner",
    "derive_seed",
]
