"""A request core builds as its own (``Attribution.OWN``) belongs to what it names.

Any signal of a step may ask for a job's step or an owned submission, so the asker's
decision is not the request's: the request keeps the decision it names and ends with it.
A sibling evaluation reducer stands in for that other asker: in the step of another
decision's measurement, it hands the production reducer the event that makes the request.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import vs_core.api as core

from .test_cancel_stop_run_jobs import _Run
from .test_measurements import committed, observation, plan, requested


@dataclass(frozen=True)
class _AskedByAnother:
    """In the step of another decision's measurement, the production reducer sees ``event``."""

    event: core.EvaluationEvent

    def __call__(
        self,
        state: core.EvaluationState,
        context: core.EvaluationContext,
        event: core.EvaluationEvent,
    ) -> core.AreaChange[core.EvaluationState]:
        if isinstance(event, core.MeasurementRequested):
            event = self.event
        return core.advance_evaluation(state, context, event)


@dataclass(frozen=True)
class _ResubmitsAnother:
    """In the step of another decision's measurement, core makes ``request``, as its own."""

    request: core.Request

    def __call__(
        self,
        state: core.EvaluationState,
        context: core.EvaluationContext,
        event: core.EvaluationEvent,
    ) -> core.AreaChange[core.EvaluationState]:
        if isinstance(event, core.MeasurementRequested):
            outbound = core.inherit(self.request).model_copy(
                update={"attribution": core.Attribution.OWN}
            )
            return core.AreaChange(state=state, requests=(outbound,))
        return core.advance_evaluation(state, context, event)


def _other_measure(state: core.CoreState) -> core.DecisionSubmitted:
    """Another decision's measurement, with a limit of its own."""
    measure = core.Measure(
        decision_id=core.DecisionId(root="another"),
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        plan=plan(evaluator_digest="evaluator-other"),
    )
    return core.DecisionSubmitted(
        decision=core.Wait(request=measure, until=core.Within(limit=1000.0)),
        expected_revision=state.revision,
    )


@pytest.mark.regression
@pytest.mark.parametrize("agent", [False, True])
def test_a_jobs_evidence_collection_names_the_jobs_decision_whoever_asks(*, agent: bool) -> None:
    """The collection core asks for once a job ends without evidence is the job's step.

    It used to take its decision from the step that asked, unlike the job's poll, inspection
    and cancel: asked in another decision's step, it named that decision.
    """
    run = _Run(1, agents=(agent,))
    run.observe(0, core.ObservationStatus.PENDING, accepted=True)
    [(resource, job)] = run.jobs.items()
    ended = observation(
        job,
        sequence=2,
        resource_id=resource,
        status=core.ObservationStatus.FAILED,
        terminal=True,
        accepted=True,
        observed_at=run.now,
    )
    state = committed(run.state, ended)
    asker = _AskedByAnother(core.JobObserved(resource_id=resource, observation=ended))
    result = core.step(state, _other_measure(state), reducers=core.CoreReducers(evaluation=asker))
    [collect] = [r for r in result.requests if isinstance(r, core.CollectEvidence)]
    assert collect.decision_id == job.decision_id


@pytest.mark.regression
def test_an_own_request_with_an_inherited_limit_ends_with_the_decision_it_names() -> None:
    """A submission of decision A, made in decision B's step, ends when A's started work does.

    The limit used to come from the step's decision, so it ended with B's instead.
    """
    asked = requested(identity="first")
    [submission] = asked.requests
    assert isinstance(submission, core.SubmitMeasurement)
    assert submission.request_id is not None
    state = core.step(asked.state, core.DispatchAuthorized(request_id=submission.request_id)).state
    [started] = [row for row in state.records.records if row.request_id == submission.request_id]
    state = core.step(state, core.ClockAdvanced(now_at=started.until.at / 2)).state
    again = submission.model_copy(
        update={"request_id": core.RequestId(root=f"{submission.request_id.root}:again")}
    )
    result = core.step(
        state,
        _other_measure(state),
        reducers=core.CoreReducers(evaluation=_ResubmitsAnother(again)),
    )
    [row] = [row for row in result.state.records.records if row.request_id == again.request_id]
    assert row.until == started.until
