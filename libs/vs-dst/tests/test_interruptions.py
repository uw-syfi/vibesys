"""A run that is killed or paused anywhere and resumed ends, or says why: it is never stuck.

The tiny world is enough to show what each class of interruption does and how the outcome of a
resume is told. The product searches are swept in `tests/system/crash/test_never_stuck.py`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_core.api import EnsureWorkspace, ResourceId
from vs_core.api.testing import Running
from vs_dst.api import (
    MAX_RESUMES,
    SAMPLE_SIZE,
    Action,
    Effects,
    Interrupted,
    Interruption,
    Moment,
    Outcome,
    simulate,
)
from vs_sim.api.testing import Depth, examples, using_depth

from ._tiny import TinyWorld

if TYPE_CHECKING:
    from vs_core.api import CoreState, Request
    from vs_core.api.testing import Answer
    from vs_dst.api import SimulatedRun


def _effects_of(normal: SimulatedRun, moment: Moment) -> int:
    return len(normal.executed_effects) if moment is Moment.IN_FLIGHT else normal.commits


@settings(deadline=None, max_examples=examples(pr=3, nightly=10), derandomize=True, database=None)
@given(items=st.integers(min_value=1, max_value=3))
def test_a_run_offers_a_sample_of_every_class_of_interruption_and_every_one_at_nightly(
    items: int,
) -> None:
    normal = simulate(TinyWorld(items))

    for action in Action:
        for moment in Moment:
            total = _effects_of(normal, moment)
            with using_depth(Depth.PR):
                sampled = [p.index for p in normal.interruptions(action, moment)]
            with using_depth(Depth.NIGHTLY):
                every = [p.index for p in normal.interruptions(action, moment)]
            assert every == list(range(total))
            assert len(sampled) == min(SAMPLE_SIZE, total)
            assert sampled == sorted(set(sampled))
            assert {sampled[0], sampled[-1]} == {0, total - 1} or len(sampled) == 1


def test_a_run_offers_each_class_of_interruption_once_by_its_action_and_moment() -> None:
    normal = simulate(TinyWorld(1))

    offered = normal.interruptions()

    assert {p.kind for p in offered} == {f"{a.value} {m.value}" for a in Action for m in Moment}
    assert str(Interruption(Action.PAUSE, Moment.IN_FLIGHT, 17)) == "pause in flight 17"
    for action in Action:
        assert {p.action for p in normal.interruptions(action)} == {action}
    for moment in Moment:
        assert {p.moment for p in normal.interruptions(moment=moment)} == {moment}


def test_an_effect_in_flight_when_the_host_dies_began_again_once_and_the_run_still_ends() -> None:
    normal = simulate(TinyWorld(2))
    assert normal.rerun_attempts == 0
    assert normal.no_effect_ran_twice()

    with using_depth(Depth.NIGHTLY):
        kills = normal.interruptions(Action.KILL, Moment.IN_FLIGHT)
    assert len(kills) == len(normal.executed_effects)
    for kill in kills:
        interrupted = normal.interrupt(kill)
        assert interrupted.outcome is Outcome.CONTINUED, (kill, interrupted.detail)
        assert interrupted.result == normal.result, kill
        assert interrupted.resumes == 1, kill
        assert interrupted.reruns <= 1, kill
        assert interrupted.every_step_finished_once(), kill
    assert normal.interrupt(kills[0]).reruns == 1


def test_a_pause_parks_the_host_that_receives_it_and_resuming_reruns_nothing() -> None:
    normal = simulate(TinyWorld(2))
    with using_depth(Depth.NIGHTLY):
        pauses = normal.interruptions(Action.PAUSE, Moment.AFTER_COMMIT)
    middle = pauses[len(pauses) // 4 : 3 * len(pauses) // 4]

    for pause in middle:
        interrupted = normal.interrupt(pause)
        assert interrupted.history[0] == "parked", pause
        assert interrupted.reruns == 0, pause


@pytest.mark.regression
@pytest.mark.parametrize("action", [Action.PAUSE, Action.STOP])
@pytest.mark.parametrize("moment", Moment)
@settings(deadline=None, max_examples=examples(pr=3, nightly=10), derandomize=True, database=None)
@given(items=st.integers(min_value=1, max_value=3), where=st.integers(min_value=0, max_value=1))
@example(items=1, where=0)
def test_a_pause_or_a_stop_due_before_the_host_opens_its_log_is_held_and_taken_after(
    action: Action, moment: Moment, items: int, where: int
) -> None:
    """Regression: a stop (and a pause) due before the first host had opened its run.log was
    submitted to a host with no inputs and failed that host, so the interruption never reached it.
    One delivery holds it until the host can take it, for every action and moment."""
    normal = simulate(TinyWorld(items))
    points = normal.interruptions(action, moment)
    point = points[0] if where == 0 else points[-1]

    interrupted = normal.interrupt(point)

    # A pause that lands as the run ends finds it ended; one that lands earlier parks it.
    taken = ("parked", "terminal") if action is Action.PAUSE else ("terminal",)
    assert interrupted.cores[0] is not None, point
    assert interrupted.history[0] in taken, (point, interrupted.history)
    assert interrupted.never_stuck(), point


class _AfterRestart(TinyWorld):
    """A world whose far side goes wrong once the run has been taken over by a second host."""

    def __init__(self, items: int = 2) -> None:
        super().__init__(items)
        self.asked_after_restart = 0

    def __call__(self, request: Request, core: CoreState) -> Answer:
        if isinstance(request, EnsureWorkspace) and core.steps.epoch > 1:
            self.asked_after_restart += 1
            return self.after_restart(request)
        return super().__call__(request, core)

    def after_restart(self, request: EnsureWorkspace) -> Answer:
        raise NotImplementedError


class _NeverAnswers(_AfterRestart):
    def after_restart(self, request: EnsureWorkspace) -> Answer:
        del request
        return Running(resource_id=ResourceId(root="workspace:unanswered"))


class _Explodes(_AfterRestart):
    def after_restart(self, request: EnsureWorkspace) -> Answer:
        del request
        message = "the far side exploded"
        raise RuntimeError(message)


_ATTEMPTS_AFTER_A_RESTART = 2
"""The step may run ``1 + request_retries`` attempts (the tiny world's limit is 2) and the killed
host's attempt is the first, charged as a crash is, so the resumes ask for the rest, which ends
the step as failed."""


def _asked_after_a_restart(interrupted: Interrupted) -> int:
    world = interrupted.world
    assert isinstance(world, _AfterRestart)
    return world.asked_after_restart


def test_a_run_still_waiting_on_a_far_side_that_never_answers_is_stuck_not_waited_for() -> None:
    normal = simulate(_NeverAnswers(1))
    kill = normal.interruptions(Action.KILL, Moment.IN_FLIGHT)[0]

    interrupted = normal.interrupt(kill)

    assert interrupted.outcome is Outcome.STUCK
    assert not interrupted.never_stuck()
    assert interrupted.resumes == MAX_RESUMES
    assert interrupted.history == ("running",) * MAX_RESUMES
    assert _asked_after_a_restart(interrupted) == _ATTEMPTS_AFTER_A_RESTART


def test_a_far_side_that_raises_after_a_resume_is_asked_within_the_retry_bound_not_failing_the_host() -> (
    None
):
    normal = simulate(_Explodes(1))
    kill = normal.interruptions(Action.KILL, Moment.IN_FLIGHT)[0]

    interrupted = normal.interrupt(kill)

    assert interrupted.outcome is Outcome.STUCK
    assert not interrupted.never_stuck()
    assert interrupted.resumes == MAX_RESUMES
    assert _asked_after_a_restart(interrupted) == _ATTEMPTS_AFTER_A_RESTART


@given(outcome=st.sampled_from(Outcome), finished=st.dictionaries(st.text(), st.integers(1, 3)))
def test_a_run_is_never_stuck_when_it_continued_or_ended_with_a_reason_and_finished_each_step_once(
    outcome: Outcome, finished: dict[str, int]
) -> None:
    point = Interruption(Action.KILL, Moment.IN_FLIGHT, 0)
    interrupted = Interrupted(
        point, outcome, "", 1, (), None, Effects(executed=[*finished], finished=finished)
    )

    assert interrupted.never_stuck() == (outcome in (Outcome.CONTINUED, Outcome.ENDED_WITH_REASON))
    assert interrupted.every_step_finished_once() == all(n == 1 for n in finished.values())


def test_only_a_run_no_interruption_has_touched_offers_interruptions() -> None:
    normal = simulate(TinyWorld(2))
    recovered = normal.rerun_with_crash(normal.crash_points()[3])

    with pytest.raises(ValueError, match="no interruption has touched"):
        recovered.interruptions()
