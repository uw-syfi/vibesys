"""``decide`` holds the close of an attempt scope until the steps of that scope have results.

The recovery page says it ("Closure waits for the scope's steps to have results instead of proving
release"), and the job runners rely on it: a close lists and cancels the jobs registered under its
scope (``vs_runtime._job_registry``), and a step that has begun but not yet registered its job when
the close reads the registry would submit that job after the close. The rule is owned by
``rr/core-fixes`` (the open question of the 6a plan, section 5, was answered by this test failing:
today ``decide`` starts the ``CloseAttemptScope`` of a retiring attempt while a step of its scope
still has a running attempt, and the attempt lists no release dependency on a measurement whose job
core has not seen). The test that states the rule is a strict xfail until that change lands; it
then fails as an unexpected pass, and its marker is removed with the change.
"""

from __future__ import annotations

import pytest

import vs_core.api as core
from vs_core.api.recovery import (
    Cancelled,
    CancelReason,
    Run,
    decide,
    finished_message,
    fold,
    input_message,
    started_message,
)
from vs_proto.api import Oneof, Record, Timestamp

from .test_attempt_retirement import fixture, retirement
from .test_measurements import plan


def record(seq: int, body: Oneof) -> Record:
    return Record(seq=seq, epoch=1, at=Timestamp(seconds=seq), clock=float(seq), body=body)


def retiring_over_a_running_submission() -> tuple[core.CoreState, core.RequestId]:
    """A retiring attempt whose measurement step has begun and has no result."""
    state, ref = fixture()
    scope = core.Scope(owner=ref.attempt_id, generation=ref.generation)
    submitted = core.step(
        state,
        core.DecisionSubmitted(
            decision=core.Wait(
                request=core.Measure(
                    decision_id=core.DecisionId(root="measure"),
                    scope=scope,
                    plan=plan(purpose="local-validation", candidate=state.run.facts.baseline),
                ),
                until=core.Within(limit=100.0),
            ),
            expected_revision=state.revision,
        ),
    )
    (submission,) = submitted.requests
    assert isinstance(submission, core.SubmitMeasurement)
    tag = submission.request_id
    assert tag is not None
    state = fold(
        [record(1, Oneof("step_started", started_message(Run(submission, tag, 1, 100.0))))],
        submitted.state,
    )
    state = fold([record(2, Oneof("core_input", input_message(retirement(ref, "cancel"))))], state)
    return state, tag


def closes(state: core.CoreState) -> list[core.CloseAttemptScope]:
    return [
        action.step
        for action in decide(state, 3.0)
        if isinstance(action, Run) and isinstance(action.step, core.CloseAttemptScope)
    ]


@pytest.mark.xfail(strict=True, reason="core-fixes")
def test_the_close_of_a_scope_is_held_while_a_step_of_the_scope_has_no_result() -> None:
    state, _ = retiring_over_a_running_submission()
    assert closes(state) == []


def test_the_close_of_a_scope_runs_once_the_steps_of_the_scope_have_results() -> None:
    state, tag = retiring_over_a_running_submission()
    ended = Cancelled(reason=CancelReason.RUN_ENDING)
    state = fold([record(3, Oneof("step_finished", finished_message(tag, 1, ended)))], state)
    assert len(closes(state)) == 1
