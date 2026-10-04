"""Shared propagation and projection contracts frozen before wave-1 leaves."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import inspect_source, value_digest
from .test_proof_ownership_regressions import stopped

TIMES = st.floats(min_value=0.0, max_value=1000.0, allow_nan=False, allow_infinity=False)


def start(state: core.CoreState) -> core.StartAttempt:
    return core.StartAttempt(
        decision_id=core.DecisionId(root="start"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=core.AttemptId(root="attempt"),
        item_id=core.ItemId(root="item"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )


def registered_attempt(
    state: core.CoreState, charges: tuple[core.ChargeReceipt, ...] = ()
) -> core.AttemptView:
    decision = start(state)
    return core.AttemptView(
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        workspace=decision.workspace,
        budget=decision.budget,
        admission_id=decision.decision_id,
        charges=charges,
    )


def observation(
    scope: core.Scope, at: float, request_id: core.RequestId | None = None
) -> core.Observation:
    return core.Observation(
        event_id=core.EventId(root="observation"),
        request_id=request_id or core.RequestId(root="request"),
        scope=scope,
        sequence=1,
        observed_at=at,
        status=core.ObservationStatus.SUCCEEDED,
    )


@given(st.lists(st.tuples(st.sampled_from(list(core.ChargeKind)), st.integers(0, 20)), max_size=20))
def test_admission_projection_uses_only_authoritative_currency_receipts(
    values: list[tuple[core.ChargeKind, int]],
) -> None:
    state = core.initial_state()
    receipts = tuple(
        core.ChargeReceipt(
            charge_id=core.ChargeId(root=f"charge:{index}"),
            kind=kind,
            charged=amount,
        )
        for index, (kind, amount) in enumerate(values)
    )
    state = state.model_copy(
        update={"attempts": core.AttemptsState(attempts=(registered_attempt(state, receipts),))}
    )
    view = core.project(state).scheduling
    assert view.charged == sum(
        amount for kind, amount in values if kind == core.ChargeKind.ADMISSION
    )
    assert view.refunded == 0


@given(admitted_at=TIMES, interval=TIMES, ended=st.booleans())
def test_occupancy_projects_ended_and_running_episodes_without_a_clock(
    admitted_at: float,
    interval: float,
    *,
    ended: bool,
) -> None:
    state = core.initial_state()
    now = admitted_at + interval
    slot = core.Slot(
        attempt=core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0),
        admission_id=core.DecisionId(root="episode"),
        admitted_at=admitted_at,
        charge_ended_at=now if ended else None,
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"now_at": now}),
            "scheduling": core.SchedulingState(slots=(slot,), released_slot_seconds=7.0),
        }
    )
    view = core.project(state).scheduling
    assert view.slot_seconds == 7.0 + (now - admitted_at if ended else 0.0)
    assert view.active_slot_seconds == (0.0 if ended else now - admitted_at)


@given(TIMES, TIMES)
def test_observation_time_advances_monotonically(current: float, supplied: float) -> None:
    state = core.initial_state()
    state = state.model_copy(update={"run": state.run.model_copy(update={"now_at": current})})
    event = core.JobObserved(
        resource_id=core.ResourceId(root="job"),
        observation=observation(core.Scope(owner=state.run.run_id, generation=0), supplied),
    )
    result = core.trace_step(
        state,
        event,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=event,
                    change=core.EvaluationChange(state=state.evaluation),
                ),
            )
        ),
    )
    assert result.state.run.now_at == max(current, supplied)


@given(st.text(min_size=1, max_size=30))
def test_settlement_forwards_exact_candidate_revision(digest: str) -> None:
    state = core.initial_state()
    attempt = core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0)
    candidate = core.RevisionRef(revision_id=core.RevisionId(root="candidate"), digest=digest)
    decision = core.Withdraw(
        decision_id=core.DecisionId(root="settle"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        target=attempt,
        disposition=core.Settle(
            outcome="succeeded",
            retention="candidate",
            candidate=candidate,
            assessments=(),
            eligible=False,
        ),
    )
    signal = core.AssessmentSubmitted(
        settlement=core.Settlement(
            settlement_id=core.SettlementId(root="settlement:settle"),
            attempt=attempt,
            candidate=candidate,
            assessments=(),
            eligible=False,
            retention="candidate",
            outcome="succeeded",
        )
    )
    result = core.trace_step(
        state,
        core.DecisionSubmitted(decision=decision, expected_revision=0),
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=signal,
                    change=core.SettlementChange(state=state.settlement),
                ),
            )
        ),
    )
    assert isinstance(result.events[0], core.Accepted)


@given(
    st.sampled_from(
        [core.RecoveryPhase.REQUIRED, core.RecoveryPhase.RECOVERING, core.RecoveryPhase.BLOCKED]
    )
)
def test_recovery_barrier_rejects_ordinary_decisions(phase: core.RecoveryPhase) -> None:
    state = core.initial_state()
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={"recovery": core.RecoveryBarrier(phase=phase)}
            )
        }
    )
    result = core.step(state, core.DecisionSubmitted(decision=start(state), expected_revision=0))
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].code == core.RejectionCode.CLOSED_SCOPE
    assert result.events[0].path == ("recovery",)
    assert result.requests == ()


def test_queued_registration_is_separate_from_slot_admission() -> None:
    state = core.initial_state()
    decision = start(state)
    request = core.AttemptRequest(
        decision_id=decision.decision_id,
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=0,
        admission_charge=1,
    )
    queued = state.scheduling.model_copy(update={"queue": (request,)})
    signal = core.AttemptRequested(request=request)
    registration = core.AttemptRegistered(
        request=request,
        workspace=decision.workspace,
        budget=decision.budget,
    )
    result = core.trace_step(
        state,
        core.DecisionSubmitted(decision=decision, expected_revision=0),
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=signal,
                    change=core.SchedulingChange(
                        state=queued, signals=(core.RegisterAttempt(request=request),)
                    ),
                ),
                core.TraceFrame(
                    signal=registration,
                    change=core.AttemptsChange(
                        state=core.AttemptsState(attempts=(registered_attempt(state),))
                    ),
                ),
            )
        ),
    )
    assert result.state.scheduling.queue == (request,)
    assert result.state.scheduling.slots == ()
    assert isinstance(result.events[0], core.Accepted)


def test_acquisition_episode_reaches_prepared_requests() -> None:
    state = core.initial_state()
    decision = start(state)
    request = core.AttemptRequest(
        decision_id=decision.decision_id,
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=0,
        admission_charge=1,
    )
    admitted = core.AttemptAdmitted(
        request=request,
        admission_id=decision.decision_id,
        workspace=decision.workspace,
        budget=decision.budget,
    )
    workspace = core.EnsureWorkspace(
        scope=core.Scope(owner=decision.attempt_id, generation=0),
        deadline_at=100.0,
        attempt=core.AttemptRef(attempt_id=decision.attempt_id, generation=0),
        plan=decision.workspace,
    )
    result = core.trace_step(
        state,
        core.DecisionSubmitted(decision=decision, expected_revision=0),
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=core.AttemptRequested(request=request),
                    change=core.SchedulingChange(
                        state=state.scheduling, signals=(core.AdmitAttempt(request=request),)
                    ),
                ),
                core.TraceFrame(
                    signal=admitted,
                    change=core.AttemptsChange(state=state.attempts, requests=(workspace,)),
                ),
            )
        ),
    )
    assert result.requests[0].admission_id == decision.decision_id
    assert result.state.intents.recovery == state.intents.recovery


@pytest.mark.parametrize("inspection", [False, True])
def test_recovery_dispatch_permits_only_inspection_and_retirement(*, inspection: bool) -> None:
    state = core.initial_state()
    decision = start(state)
    request_id = core.RequestId(root="dispatch")
    request = (
        core.InspectRequest(
            request_id=request_id,
            scope=decision.scope,
            deadline_at=100.0,
            target=core.RequestId(root="target"),
        )
        if inspection
        else core.EnsureWorkspace(
            request_id=request_id,
            scope=decision.scope,
            deadline_at=100.0,
            attempt=core.AttemptRef(attempt_id=decision.attempt_id, generation=0),
            plan=decision.workspace,
        )
    )
    preparation = core.ClockAdvanced(now_at=0.0)
    state = core.trace_step(
        state,
        preparation,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=preparation,
                    change=core.SchedulingChange(state=state.scheduling, requests=(request,)),
                ),
            )
        ),
    ).state
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={"recovery": core.RecoveryBarrier(phase=core.RecoveryPhase.BLOCKED)}
            )
        }
    )
    event = core.DispatchAuthorized(request_id=request_id)
    if inspection:
        with pytest.raises(core.KernelNotImplementedError):
            core.step(state, event)
    else:
        with pytest.raises(core.ContractError, match="recovery"):
            core.step(state, event)


def test_recovery_ready_wakes_scheduling_without_unpausing() -> None:
    state = core.initial_state()
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"status": core.RunStatus.PAUSED})}
    )
    started = core.RecoveryStarted(now_at=5.0, epoch=2)
    ready = state.intents.model_copy(
        update={"recovery": core.RecoveryBarrier(epoch=2, phase=core.RecoveryPhase.READY)}
    )
    wake = core.ClockAdvanced(now_at=5.0)
    result = core.trace_step(
        state,
        started,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=started,
                    change=core.IntentsChange(state=ready, signals=(core.RecoveryReady(epoch=2),)),
                ),
                core.TraceFrame(signal=wake, change=core.SchedulingChange(state=state.scheduling)),
            )
        ),
    )
    assert result.state.run.status == core.RunStatus.PAUSED
    assert result.state.intents.recovery == ready.recovery


@given(terminal=st.booleans(), released=st.booleans(), complete=st.booleans())
def test_child_leases_fence_run_closure_until_exact_release_manifest(
    *,
    terminal: bool,
    released: bool,
    complete: bool,
) -> None:
    state = stopped(core.initial_state())
    scope = core.Scope(owner=state.run.run_id, generation=0)
    observed = observation(scope, 5.0).model_copy(
        update={
            "resource_id": core.ResourceId(root="child"),
            "terminal": terminal,
            "released": released,
            "children_complete": complete,
        }
    )
    child = core.ChildLease(
        resource_id=core.ResourceId(root="child"),
        scope=scope,
        source_requests=(observed.request_id,),
        observation=observed,
        observation_watermarks=(
            core.ChildObservationWatermark(
                source_request=observed.request_id, observation=observed
            ),
        ),
        watermark_history_complete=True,
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": core.RunResultProposal(outcome="cancelled", reason="cleanup"),
                }
            ),
            "intents": state.intents.model_copy(
                update={
                    "children": (child,),
                    "intents": (inspect_source(observed.request_id, scope),),
                }
            ),
        }
    )
    clock = core.ClockAdvanced(now_at=5.0)
    result = core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(
                        state=state.scheduling, signals=(core.RunDrained(),)
                    ),
                ),
            )
        ),
    )
    expected = (
        core.RunStatus.TERMINAL if terminal and released and complete else core.RunStatus.CLOSING
    )
    assert result.state.run.status == expected


def test_queued_retirement_binds_exact_registration_generation() -> None:
    state = core.initial_state()
    first = start(state)
    current = first.model_copy(
        update={
            "decision_id": core.DecisionId(root="current"),
            "scope": core.Scope(owner=state.run.run_id, generation=1),
        }
    )
    owner = registered_attempt(state).model_copy(
        update={"generation": 1, "phase": core.AttemptPhase.QUEUED, "admission_id": None}
    )
    receipts = tuple(
        core.DecisionReceipt(
            decision_id=decision.decision_id,
            decision=decision,
            payload_digest=value_digest(decision),
            feedback=core.Accepted(decision_id=decision.decision_id),
        )
        for decision in (first, current)
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"generation": 1, "receipts": receipts}),
            "attempts": core.AttemptsState(attempts=(owner,)),
        }
    )
    target = core.AttemptRef(attempt_id=owner.attempt_id, generation=1)
    decision = core.Withdraw(
        decision_id=core.DecisionId(root="withdraw"),
        scope=current.scope,
        target=target,
        disposition=core.Cancel(),
    )
    signal = core.RetireRequested(
        attempt=target,
        disposition="cancel",
        authority=core.RequestId(root="withdraw:withdraw"),
        admission_id=current.decision_id,
        requested_at=0.0,
    )
    result = core.trace_step(
        state,
        core.DecisionSubmitted(decision=decision, expected_revision=0),
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=signal,
                    change=core.AttemptsChange(state=state.attempts),
                ),
            )
        ),
    )
    assert isinstance(result.events[0], core.Accepted)


def test_queued_retirement_without_registration_fails_with_typed_error() -> None:
    state = core.initial_state()
    owner = registered_attempt(state).model_copy(
        update={"admission_id": None, "phase": core.AttemptPhase.QUEUED}
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    decision = core.Withdraw(
        decision_id=core.DecisionId(root="withdraw"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        target=core.AttemptRef(attempt_id=owner.attempt_id, generation=0),
        disposition=core.Cancel(),
    )
    with pytest.raises(core.ContractError, match="canonical accepted registration"):
        core.step(state, core.DecisionSubmitted(decision=decision, expected_revision=0))
