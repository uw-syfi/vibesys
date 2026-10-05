"""Agent in-turn evaluation calls are admitted by core under the same budget as Measure."""

from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_measurements import committed, observation, plan, requested, transition


def call(
    state: core.CoreState,
    call_id: str,
    *,
    variant: int = 0,
    generation_offset: int = 0,
) -> core.Transition:
    scope = core.Scope(owner=state.run.run_id, generation=state.run.generation + generation_offset)
    event = core.AgentMeasurementRequested(
        scope=scope, plan=plan(evaluator_digest=f"evaluator-{variant}"), call_id=call_id
    )
    return transition(state, event)


def submissions(state: core.CoreState) -> list[core.SubmitMeasurement]:
    return [
        row.request
        for row in state.intents.intents
        if isinstance(row.request, core.SubmitMeasurement)
    ]


def test_call_allocates_one_submission_without_a_decision() -> None:
    result = call(core.initial_state(), "c1")
    assert len(result.requests) == 1
    request = result.requests[0]
    assert isinstance(request, core.SubmitMeasurement)
    assert request.decision_id is None
    assert result.state.evaluation.agent_calls == (
        core.AgentCall(call_id="c1", scope=request.scope, request_id=request.request_id),
    )
    budget = result.state.evaluation.submission_budgets[0]
    assert [r.request_id for r in budget.receipts] == [request.request_id]


def test_replayed_call_changes_nothing() -> None:
    first = call(core.initial_state(), "c1")
    replay = call(first.state, "c1")
    assert replay.requests == ()
    assert replay.events == ()
    assert replay.state.evaluation == first.state.evaluation


def test_stale_generation_is_rejected_and_recorded() -> None:
    result = call(core.initial_state(), "c1", generation_offset=1)
    assert result.requests == ()
    assert [e.status for e in result.events if isinstance(e, core.MeasurementResult)] == [
        core.ObservationStatus.REJECTED
    ]
    assert result.state.evaluation.agent_calls[0].request_id is None
    assert result.state.evaluation.submission_budgets == ()


def test_agent_call_and_measure_decision_share_one_budget() -> None:
    measured = requested(measurement=plan(evaluator_digest="evaluator-0", submission_limit=1))
    result = call(measured.state, "c1")
    assert result.requests == ()
    assert len(submissions(result.state)) == 1


@given(
    calls=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=5),
            st.integers(min_value=0, max_value=2),
            st.booleans(),
            st.booleans(),
        ),
        min_size=1,
        max_size=25,
    )
)
def test_any_call_sequence_never_exceeds_budget_or_duplicates_a_measurement(
    calls: list[tuple[int, int, bool, bool]],
) -> None:
    """Calls repeat ids, vary identity, go stale, and interleave with failed submissions."""
    state = core.initial_state()
    limit = state.run.limits.max_measurement_submissions
    seen_ids: set[str] = set()
    for call_number, (call_id, variant, stale, fail_previous) in enumerate(calls):
        if fail_previous and (rows := submissions(state)):
            last = rows[-1]
            observed = observation(
                last,
                call_number + 1,
                accepted=False,
                terminal=True,
                status=core.ObservationStatus.FAILED,
                resource_id=None,
            )
            state = committed(state, observed)
            state = transition(
                state,
                core.MeasurementSubmissionObserved(
                    observation=observed, failure=core.MeasurementFailure.INFRASTRUCTURE
                ),
            ).state
        known = {c.call_id for c in state.evaluation.agent_calls}
        result = call(state, f"call-{call_id}", variant=variant, generation_offset=int(stale))
        if f"call-{call_id}" in known:
            assert result.requests == ()
            assert result.state.evaluation == state.evaluation
        if stale:
            assert result.requests == ()
        for request in result.requests:
            assert request.request_id is not None
            assert request.request_id.root not in seen_ids
            seen_ids.add(request.request_id.root)
        state = result.state
        for budget in state.evaluation.submission_budgets:
            assert len(budget.receipts) <= min(budget.limit, limit)
    assert len(seen_ids) == len(submissions(state))
