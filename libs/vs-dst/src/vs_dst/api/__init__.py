"""Simulation tests of the VibeSys run lifecycle with cheap crash sweeps (test support).

A test builds a `World` (the strategy under test and the far side of its run), calls `simulate`
to get a `SimulatedRun`, and asserts on it. For every `CrashPoint` of that run,
`rerun_with_crash` shows what a restarted host makes of it, starting from a copy of the
outside world at that point instead of running the prefix again::

    normal = simulate(world)
    for crash in normal.crash_points():
        recovered = normal.rerun_with_crash(crash)
        assert recovered.result == normal.result, crash
        assert recovered.no_effect_ran_twice(), crash
"""

from vs_dst._crash_points import (
    EVERY_POINT,
    SAMPLE_SIZE,
    SAMPLED,
    CrashKind,
    CrashPoint,
    CrashPointPolicy,
    crash_point_policy,
    set_crash_point_policy,
    using_crash_point_policy,
)
from vs_dst._effects import Effects, RecoverableExecutors
from vs_dst._simulate import SimulatedRun, simulate
from vs_dst._snapshots import NondeterministicWorldError, WorldNotCopyableError
from vs_dst._world import RunSetup, World

__all__ = [
    "EVERY_POINT",
    "SAMPLED",
    "SAMPLE_SIZE",
    "CrashKind",
    "CrashPoint",
    "CrashPointPolicy",
    "Effects",
    "NondeterministicWorldError",
    "RecoverableExecutors",
    "RunSetup",
    "SimulatedRun",
    "World",
    "WorldNotCopyableError",
    "crash_point_policy",
    "set_crash_point_policy",
    "simulate",
    "using_crash_point_policy",
]
