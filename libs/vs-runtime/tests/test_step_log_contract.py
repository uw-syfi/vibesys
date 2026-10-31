"""Every ``StepLog`` passes the contract; the Fake folds core's own records, so it is held to it."""

from __future__ import annotations

import vs_core.api as core
from vs_core.api.recovery import Run, decide
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
