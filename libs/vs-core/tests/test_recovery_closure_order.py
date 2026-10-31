"""Closing an attempt does not wait for the open steps of its scope, nor stop one that has not begun.

Answer to the open question of the 6a plan (section 5): no. ``decide`` starts the
``CloseAttemptScope`` of a retiring attempt while a step of the same scope still has a running
attempt, because no dependency orders them and ``request_effect`` classes the close as neither a
writer nor a snapshot. The attempt itself lists no release dependency on that step either: it
depends on the closing request and on the jobs and records it already knows, so a measurement whose
job core has not yet seen (a step reports its job only when it ends) is not among them. A step of
the scope that has not begun is neither started nor settled by the retirement: it waits for the
run's own end.

What makes it safe for a running job is the close itself, not an ordering: it cancels every job
registered under the scope and lists it (``vs_runtime._job_registry``), so the step ends. What no
rule covers is a step that has begun but not yet registered its job when the close reads the
registry: its job is submitted after the close. The window is the time between ``StepStarted`` and
the job's registration, the first thing a job runner does.
"""

from __future__ import annotations

import vs_core.api as core
from vs_core.api.recovery import (
    Cancel,
    Run,
    Settle,
    decide,
    fold,
    input_message,
    started_message,
)
from vs_proto.api import Oneof, Record, Timestamp

from .test_attempt_retirement import fixture, retirement
from .test_measurements import plan


def record(seq: int, body: Oneof) -> Record:
    return Record(seq=seq, epoch=1, at=Timestamp(seconds=seq), clock=float(seq), body=body)


def closing_over_a_running_submission(
    *, begun: bool = True
) -> tuple[core.CoreState, core.SubmitMeasurement, int]:
    """A retiring attempt whose measurement step has begun (or not), and the log's length."""
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
    assert submission.request_id is not None
    state = submitted.state
    if begun:
        run = Run(submission, submission.request_id, 1, 100.0)
        state = fold([record(1, Oneof("step_started", started_message(run)))], state)
    event = retirement(ref, "cancel")
    state = fold([record(2, Oneof("core_input", input_message(event)))], state)
    return state, submission, 2


def test_the_close_of_a_scope_starts_while_a_step_of_the_scope_still_runs() -> None:
    state, submission, _ = closing_over_a_running_submission()
    actions = decide(state, 3.0)
    runs = [action.step for action in actions if isinstance(action, Run)]
    assert [type(step) for step in runs] == [core.CloseAttemptScope]
    assert not [action for action in actions if isinstance(action, Cancel)]
    assert submission.request_id is not None


def test_the_attempt_lists_no_release_dependency_on_a_step_whose_job_core_has_not_seen() -> None:
    state, submission, _ = closing_over_a_running_submission()
    (owner,) = state.attempts.attempts
    assert owner.phase is core.AttemptPhase.CLOSING
    assert {dependency.identity for dependency in owner.release_dependencies} == {
        core.RequestId(root="withdraw")
    }
    assert submission.request_id not in {item.identity for item in owner.release_dependencies}


def test_a_step_of_the_scope_that_has_not_begun_is_not_started_and_not_settled_by_the_retirement() -> (
    None
):
    state, submission, _ = closing_over_a_running_submission(begun=False)
    actions = decide(state, 3.0)
    assert [type(action.step) for action in actions if isinstance(action, Run)] == [
        core.CloseAttemptScope
    ]
    assert not [action for action in actions if isinstance(action, Settle | Cancel)]
    assert submission in {row.request for row in state.records.records}
