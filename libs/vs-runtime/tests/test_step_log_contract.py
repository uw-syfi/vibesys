"""Every ``StepLog`` passes the contract: the Fake, and the production log with and without state.pb.

The Fake folds core's own records, so it is held to the contract. ``RunLogStepLog`` is what a run
appends through, and runs the same cases over a log it opens itself, so the cache it writes beside
the log changes none of what the loop relies on.
"""

from __future__ import annotations

import vs_core.api as core
from vs_core.api.recovery import Run, decide
from vs_proto.api import EpochStarted, Timestamp
from vs_runlog.api import ConfirmedLog, MemoryFoldCache
from vs_runtime.api.step_runner import RunLogStepLog
from vs_runtime.api.testing import (
    FakeRunRecords,
    FakeStepLog,
    StepLogContract,
    StepLogUnderTest,
    fake_run_seed,
)
from vs_sim.api.testing import SimThreads, VirtualClock, run_virtual


class TestFakeStepLog(StepLogContract):
    def step_log_under_test(self) -> StepLogUnderTest:
        clock = VirtualClock()
        seed = fake_run_seed([30.0, 40.0], retries=2)
        records = FakeRunRecords()
        log = FakeStepLog(records, seed.state, seed.registry, clock, SimThreads())

        def reopen() -> FakeStepLog:
            return FakeStepLog(records, seed.state, seed.registry, clock, SimThreads())

        def push_input() -> None:
            control = core.ControlInput(control_id=core.ControlId(root="input"), action="pause_now")
            log.push(core.RunControlEvent(control=control, now_at=clock.now()))

        def pause() -> None:
            control = core.ControlInput(control_id=core.ControlId(root="pause"), action="pause_now")
            log.push(core.RunControlEvent(control=control, now_at=clock.now()))

        return StepLogUnderTest(
            log=log,
            reopen=reopen,
            push_input=push_input,
            pause=pause,
            elapse=clock.sleep,
            drive=lambda main: run_virtual(clock, main),
            runs=tuple(a for a in decide(seed.state, clock.now()) if isinstance(a, Run)),
        )


class RunLogProcesses:
    """Processes over one run.log in memory, each opening the epoch after the last."""

    def __init__(self, *, cached: bool) -> None:
        self.clock = VirtualClock()
        self.seed = fake_run_seed([30.0, 40.0], retries=2)
        self._records = FakeRunRecords()
        self._cache = MemoryFoldCache() if cached else None

    def start(self) -> RunLogStepLog:
        confirmed = ConfirmedLog(self._records, SimThreads())
        epoch = (self._records.records[-1].epoch if self._records.records else 0) + 1
        confirmed.append(
            EpochStarted(epoch=epoch), epoch=epoch, at=Timestamp(), clock=self.clock.now()
        )
        return RunLogStepLog.open(
            confirmed,
            epoch=epoch,
            seed=self.seed.state,
            registry=self.seed.registry,
            clock=self.clock,
            cache=self._cache,
            cache_every=1,
        )


def run_log_under_test(*, cached: bool) -> StepLogUnderTest:
    processes = RunLogProcesses(cached=cached)
    clock = processes.clock
    log = processes.start()

    def push(name: str) -> None:
        control = core.ControlInput(control_id=core.ControlId(root=name), action="pause_now")
        log.input(core.RunControlEvent(control=control, now_at=clock.now()))

    return StepLogUnderTest(
        log=log,
        reopen=processes.start,
        push_input=lambda: push("input"),
        pause=lambda: push("pause"),
        elapse=clock.sleep,
        drive=lambda main: run_virtual(clock, main),
        runs=tuple(a for a in decide(processes.seed.state, clock.now()) if isinstance(a, Run)),
    )


class TestRunLogStepLog(StepLogContract):
    def step_log_under_test(self) -> StepLogUnderTest:
        return run_log_under_test(cached=False)


class TestRunLogStepLogWithStateCache(StepLogContract):
    def step_log_under_test(self) -> StepLogUnderTest:
        return run_log_under_test(cached=True)
