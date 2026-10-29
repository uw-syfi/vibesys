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
        scope=scope,
        plan=plan(evaluator_digest=f"evaluator-{variant}"),
        within=core.Within(limit=100.0),
        call_id=call_id,
    )
    return transition(state, event)


def submissions(state: core.CoreState) -> list[core.SubmitMeasurement]:
    return [
        row.request
        for row in state.records.records
        if isinstance(row.request, core.SubmitMeasurement)
    ]


def limit_of(state: core.CoreState, request_id: core.RequestId | None) -> float:
    """The absolute time core stored for the submission with this identity."""
    (record,) = [r for r in state.records.records if r.request_id == request_id]
    return record.until.at


def test_call_allocates_one_submission_without_a_decision() -> None:
    result = call(core.initial_state(), "c1")
    assert len(result.requests) == 1
    request = result.requests[0]
    assert isinstance(request, core.SubmitMeasurement)
    assert request.decision_id is None
    assert result.state.evaluation.agent_calls == (
        core.AgentCall(
            call_id="c1",
            scope=request.scope,
            request_id=request.request_id,
            deadline_at=limit_of(result.state, request.request_id),
        ),
    )
    budget = result.state.evaluation.submission_budgets[0]
    assert [getattr(r, "request_id", None) for r in budget.receipts] == [request.request_id]


def test_replayed_call_changes_nothing() -> None:
    first = call(core.initial_state(), "c1")
    replay = call(first.state, "c1")
    assert replay.requests == ()
    assert replay.events == ()
    assert replay.state.evaluation == first.state.evaluation


def test_stale_generation_is_rejected_and_recorded() -> None:
    result = call(core.initial_state(), "c1", generation_offset=1)
    assert result.requests == ()
    assert result.events == (), "a refused tool call is the agent's to hear, not the strategy's"
    assert result.state.evaluation.agent_calls[0].request_id is None
    assert result.state.evaluation.submission_budgets == ()


def test_agent_call_and_measure_decision_share_one_budget() -> None:
    """The agent's call for an identity a Measure already charged is that submission, not a second."""
    measured = requested(measurement=plan(evaluator_digest="evaluator-0"))
    (measure,) = submissions(measured.state)
    result = call(measured.state, "c1")
    assert result.requests == ()
    assert len(submissions(result.state)) == 1
    (row,) = result.state.evaluation.agent_calls
    assert (row.request_id, row.repeat, row.rejection) == (measure.request_id, True, None)
    assert row.deadline_at == limit_of(result.state, measure.request_id)


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
    limit = core.MAX_SUBMISSIONS_PER_MEASUREMENT
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


def rejection(state: core.CoreState, call_id: str) -> core.AgentRejection | None:
    (row,) = [c for c in state.evaluation.agent_calls if c.call_id == call_id]
    return row.rejection


def test_each_refusal_names_its_reason_and_charges_nothing() -> None:
    stale = call(core.initial_state(), "c1", generation_offset=1)
    assert rejection(stale.state, "c1") is core.AgentRejection.NOT_ADMITTED

    state = core.initial_state()
    event = core.AgentMeasurementRequested(
        scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
        plan=plan(),
        within=core.Within(limit=0.0),
        call_id="c1",
    )
    refused = transition(state, event)
    assert rejection(refused.state, "c1") is core.AgentRejection.NOT_ALLOWED
    assert submissions(refused.state) == []
    assert refused.events == ()


@given(
    status=st.sampled_from(
        [s for s in core.RunStatus if s not in (core.RunStatus.RUNNING, core.RunStatus.PAUSED)]
    ),
    calls=st.integers(min_value=1, max_value=4),
)
def test_a_run_that_is_closed_to_its_work_starts_no_agent_measurement(
    status: core.RunStatus, calls: int
) -> None:
    """Cancel, stop and deadline close the run to new work: every later call is refused.

    A paused run is not closed: a call from the turn still running when it was paused is
    that turn's own work (``test_agent_turn_runs`` pauses before every answer).
    """
    state = core.initial_state()
    state = state.model_copy(update={"run": state.run.model_copy(update={"status": status})})
    for number in range(calls):
        result = call(state, f"c{number}")
        assert result.requests == ()
        assert rejection(result.state, f"c{number}") is core.AgentRejection.RUN_STOPPING
        state = result.state
    assert submissions(state) == []
    assert state.evaluation.submission_budgets == ()


def charged(state: core.CoreState) -> list[str]:
    return [
        row.request_id.root
        for row in state.records.records
        if isinstance(row.request, core.SubmitMeasurement) and row.request_id is not None
    ]


@given(
    calls=st.lists(
        st.tuples(st.integers(min_value=0, max_value=2), st.integers(min_value=0, max_value=1)),
        min_size=1,
        max_size=14,
    )
)
def test_a_repeated_candidate_is_one_charge_and_one_handle(calls: list[tuple[int, int]]) -> None:
    """Whatever the order of calls for candidates (variants) in two scopes, one charge each.

    A call for a candidate its scope already evaluates names that submission and charges
    nothing; another variant is another charge; another scope's call is another charge.
    """
    state = core.initial_state()
    first: dict[tuple[int, int], str] = {}
    for number, (variant, generation_offset) in enumerate(calls):
        result = call(state, f"c{number}", variant=variant, generation_offset=generation_offset)
        (row,) = [c for c in result.state.evaluation.agent_calls if c.call_id == f"c{number}"]
        key = (variant, generation_offset)
        if generation_offset:
            assert row.rejection is core.AgentRejection.NOT_ADMITTED
        elif key in first:
            assert result.requests == ()
            assert (row.rejection, row.repeat, row.request_id.root if row.request_id else None) == (
                None,
                True,
                first[key],
            )
        else:
            assert len(result.requests) == 1
            assert row.request_id is not None
            assert not row.repeat
            first[key] = row.request_id.root
        assert not result.events
        state = result.state
    assert sorted(charged(state)) == sorted(first.values())


def ended(
    state: core.CoreState, request: core.SubmitMeasurement
) -> tuple[core.Transition, core.Transition]:
    """The submission accepted, then its job ending successfully: (accepted, ended)."""
    started = observation(request)
    state = committed(state, started)
    accepted = transition(state, core.MeasurementSubmissionObserved(observation=started))
    running = transition(
        accepted.state,
        core.JobObserved(resource_id=core.ResourceId(root="job"), observation=started),
    )
    finished = observation(
        request, 2, status=core.ObservationStatus.SUCCEEDED, terminal=True, released=True
    )
    done = transition(
        committed(running.state, finished),
        core.JobObserved(resource_id=core.ResourceId(root="job"), observation=finished),
    )
    return accepted, done


def results(change: core.Transition) -> list[core.MeasurementResult]:
    return [e for e in change.events if isinstance(e, core.MeasurementResult)]


def test_the_end_of_an_agents_job_is_not_reported_to_the_strategy_but_a_measures_is() -> None:
    """The v13 round with no metric: a Measure awaiting its result took the agent job's.

    The strategy takes a result of its scope for the Measure it awaits. An agent's own
    submission shares that scope, so reporting its end would hand the Measure another
    submission's (local-validation) evidence; the agent hears of its job through its
    continuation instead.
    """
    agent_state = call(core.initial_state(), "c1").state
    (agent_request,) = submissions(agent_state)
    _, agent_done = ended(agent_state, agent_request)
    assert results(agent_done) == []
    assert agent_done.state.evaluation.jobs[0].terminal

    measured = requested(measurement=plan(evaluator_digest="evaluator-0"))
    (measure_request,) = submissions(measured.state)
    _, measure_done = ended(measured.state, measure_request)
    assert [r.status for r in results(measure_done)] == [core.ObservationStatus.SUCCEEDED]
