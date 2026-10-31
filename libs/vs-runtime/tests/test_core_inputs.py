"""What reaches core besides the results of steps: the operator, the deadline, the clock, and the report of a stall.

``CoreInputs`` wraps the run's step log. A control core refuses is told to the operator and never
swallowed; a deadline stops the run once; a run that waits on nothing is reported by name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import vs_core.api as core
from vs_core.api.recovery import Interruption, Run, RunRest, at_rest, decide
from vs_runtime.api.step_runner import CoreInputs
from vs_runtime.api.testing import FakeRunRecords, FakeStepLog, fake_run_seed
from vs_sim.api.testing import SimThreads, VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass
class OperatorOf:
    """An operator that sends the controls it is given, and keeps what the run told it."""

    controls: list[core.RunControlEvent] = field(default_factory=list)
    refused: list[core.ControlRefusedError] = field(default_factory=list)
    settled_with: list[core.RunStatus] = field(default_factory=list)

    def poll(self, state: core.CoreState, now_at: float) -> tuple[core.CoreEvent, ...]:
        del state, now_at
        sent, self.controls = tuple(self.controls), []
        return sent

    def settled(
        self,
        state: core.CoreState,
        *,
        running: bool,
        refused: Sequence[core.ControlRefusedError] = (),
    ) -> None:
        del running
        self.settled_with.append(state.run.status)
        self.refused.extend(refused)


def resume_event(now_at: float) -> core.RunControlEvent:
    """A resume, which core refuses while the run is not paused: there is nothing to resume."""
    control = core.ControlInput(
        control_id=core.ControlId(root="resume-1"), action="resume", when="now"
    )
    return core.RunControlEvent(control=control, now_at=now_at)


def inputs_over(
    limits_s: list[float], operator: OperatorOf | None = None
) -> tuple[CoreInputs, FakeStepLog, VirtualClock]:
    clock = VirtualClock()
    seed = fake_run_seed(limits_s, retries=1)
    log = FakeStepLog(FakeRunRecords(), seed.state, seed.registry, clock, SimThreads())
    return CoreInputs(log, None, clock, controls=operator), log, clock


def test_a_control_core_refuses_is_told_to_the_operator_and_is_not_in_the_log() -> None:
    """A pause is terminal for its epoch: a resume sent in it is refused, and the operator hears it."""
    operator = OperatorOf()
    inputs, log, clock = inputs_over([30.0], operator)
    pause = core.ControlInput(control_id=core.ControlId(root="pause-1"), action="pause_now")
    operator.controls.append(core.RunControlEvent(control=pause, now_at=clock.now()))

    async def main() -> None:
        run = next(iter(_runs(log)))
        await inputs.step_started(run)
        inputs.stalled()  # looks at what is due, which brings the pause in
        await inputs.attempt_interrupted(run.tag, run.attempt, Interruption.PAUSE)
        assert at_rest(log.state) is RunRest.PARKED
        operator.controls.append(resume_event(clock.now()))
        inputs.stalled()

    run_virtual(clock, main())

    assert [error.control.control_id.root for error in operator.refused] == ["resume-1"]
    assert not any(c.control_id.root == "resume-1" for c in log.state.run.controls)
    assert at_rest(log.state) is RunRest.PARKED


def _runs(log: FakeStepLog) -> list[Run]:
    return [a for a in decide(log.state, 0.0) if isinstance(a, Run)]


def test_a_run_that_waits_on_nothing_is_reported_with_what_it_has_open() -> None:
    inputs, log, clock = inputs_over([])

    why = inputs.stalled()

    assert why is not None
    assert "stalled" in why
    assert log.state.run.run_id.root in why
    assert at_rest(log.state) is None
    del clock


def test_a_paused_run_is_not_a_stall_it_waits_for_its_operator() -> None:
    operator = OperatorOf()
    inputs, log, clock = inputs_over([30.0], operator)
    control = core.ControlInput(control_id=core.ControlId(root="pause-1"), action="pause_now")
    operator.controls.append(core.RunControlEvent(control=control, now_at=clock.now()))

    async def main() -> None:
        await inputs.step_started(next(iter(_runs(log))))
        await inputs.attempt_interrupted(
            next(iter(log.state.steps.attempts)).tag, 1, Interruption.PAUSE
        )

    run_virtual(clock, main())

    assert inputs.stalled() is None
