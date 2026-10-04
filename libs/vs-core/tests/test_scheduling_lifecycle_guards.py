"""Accepted FIFO progress and deadline disposition through the public kernel."""

from __future__ import annotations

from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest


def _queued_state(count: int, charge: int, generation: int) -> core.CoreState:
    state = core.initial_state()
    requests = tuple(
        core.AttemptRequest(
            decision_id=core.DecisionId(root=f"accepted-{index}"),
            attempt_id=core.AttemptId(root=f"attempt-{index}"),
            item_id=core.ItemId(root=f"item-{index}"),
            generation=generation,
            admission_charge=charge,
        )
        for index in range(count)
    )
    owners = tuple(
        core.AttemptView(
            attempt_id=request.attempt_id,
            item_id=request.item_id,
            generation=request.generation,
            phase=core.AttemptPhase.QUEUED,
            workspace=core.WorkspacePlan(
                mode=core.WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline
            ),
            budget=core.AttemptBudget(admission_charge=charge),
            charges=(
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root=f"charge-{index}"),
                    kind=core.ChargeKind.ADMISSION,
                    charged=charge,
                ),
            ),
        )
        for index, request in enumerate(requests)
    )
    starts = tuple(
        core.StartAttempt(
            decision_id=request.decision_id,
            scope=core.Scope(owner=state.run.run_id, generation=generation),
            attempt_id=request.attempt_id,
            item_id=request.item_id,
            workspace=owner.workspace,
            budget=owner.budget,
        )
        for request, owner in zip(requests, owners, strict=True)
    )
    receipts = tuple(
        core.DecisionReceipt(
            decision_id=decision.decision_id,
            decision=decision,
            payload_digest=value_digest(decision),
            feedback=core.Accepted(decision_id=decision.decision_id),
        )
        for decision in starts
    )
    return state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=owners),
            "scheduling": core.SchedulingState(queue=requests),
            "run": state.run.model_copy(
                update={
                    "generation": generation,
                    "limits": core.Limits(max_attempts=count * charge, max_parallel=1),
                    "receipts": receipts,
                }
            ),
        }
    )


def _reload(state: core.CoreState) -> core.CoreState:
    return core.CoreState.model_validate_json(state.model_dump_json())


def _assert_attempts_signal(state: core.CoreState, event: core.CoreEvent, event_kind: str) -> None:
    """Prove dispatch at the real Attempts boundary or its completed transition."""
    before = state.model_dump_json()
    for source in (state, _reload(state)):
        failure: core.KernelNotImplementedError | None = None
        result: core.Transition | None = None
        try:
            result = core.step(source, event)
        except core.KernelNotImplementedError as error:
            failure = error
        if failure is not None:
            assert failure.area == core.Area.ATTEMPTS
            assert failure.event_kind == event_kind
        else:
            assert result is not None
            head = state.scheduling.queue[0]
            assert isinstance(head, core.AttemptRequest)
            if event_kind == "attempt_admitted":
                assert any(
                    slot.attempt.attempt_id == head.attempt_id
                    and slot.admission_id == head.decision_id
                    for slot in result.state.scheduling.slots
                )
                assert head not in result.state.scheduling.queue
            else:
                owner = next(
                    owner
                    for owner in result.state.attempts.attempts
                    if owner.attempt_id == head.attempt_id and owner.generation == head.generation
                )
                assert owner.closure is not None
                assert owner.closure.disposition == "cancel"
                assert owner.closure.admission_id == head.decision_id
                assert owner.closure.authority == core.RequestId(
                    root=f"deadline:{head.decision_id.root}"
                )
            assert result.state.run.receipts == state.run.receipts
    assert state.model_dump_json() == before


@given(
    count=st.integers(1, 6),
    charge=st.integers(1, 5),
    generation=st.integers(0, 5),
    deadline=st.integers(1, 1000),
    delay=st.integers(0, 1000),
)
@example(count=1, charge=1, generation=0, deadline=10, delay=0)
def test_expired_accepted_fifo_requests_retirement_and_can_drain(
    count: int, charge: int, generation: int, deadline: int, delay: int
) -> None:
    """Every expired accepted head gets retirement, preserving receipt accounting."""
    state = _queued_state(count + 1, charge, generation)
    held, *queued = state.scheduling.queue
    assert isinstance(held, core.AttemptRequest)
    stop = core.Stop(
        decision_id=core.DecisionId(root="drain-stop"),
        scope=core.Scope(owner=state.run.run_id, generation=generation),
        mode="drain",
        result=core.RunResultProposal(outcome="cancelled", reason="search finished"),
    )
    state = state.model_copy(
        update={
            "scheduling": core.SchedulingState(
                queue=tuple(queued),
                slots=(
                    core.Slot(
                        attempt=core.AttemptRef(
                            attempt_id=held.attempt_id, generation=held.generation
                        ),
                        admission_id=held.decision_id,
                        admitted_at=0.0,
                    ),
                ),
            ),
            "attempts": core.AttemptsState(
                attempts=(
                    state.attempts.attempts[0].model_copy(
                        update={"phase": core.AttemptPhase.ACTIVE, "admission_id": held.decision_id}
                    ),
                    *state.attempts.attempts[1:],
                )
            ),
            "run": state.run.model_copy(update={"deadline_at": float(deadline)}),
        }
    )
    state = core.step(
        _reload(state), core.DecisionSubmitted(decision=stop, expected_revision=state.revision)
    ).state
    # Matching release occurs at the deadline, after the held attempt's cleanup.
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"now_at": float(deadline + delay)}),
            "attempts": core.AttemptsState(
                attempts=(
                    state.attempts.attempts[0].model_copy(
                        update={"phase": core.AttemptPhase.TERMINAL}
                    ),
                    *state.attempts.attempts[1:],
                )
            ),
        }
    )
    release = core.SlotReleased(
        attempt=core.AttemptRef(attempt_id=held.attempt_id, generation=held.generation),
        admission_id=held.decision_id,
    )
    _assert_attempts_signal(state, release, "retire_requested")
    _assert_attempts_signal(state, core.ClockAdvanced(now_at=state.run.now_at), "retire_requested")
    for request in queued:
        singleton = state.model_copy(
            update={"scheduling": state.scheduling.model_copy(update={"queue": (request,)})}
        )
        _assert_attempts_signal(
            singleton, core.ClockAdvanced(now_at=state.run.now_at), "retire_requested"
        )
    # Supply immutable Attempts-owned retirement proofs. Scheduling must retain
    # the queue until these arrive, then make RunDrained reachable exactly once.
    state = state.model_copy(
        update={"scheduling": state.scheduling.model_copy(update={"slots": ()})}
    )
    receipts = state.run.receipts
    charges = tuple(owner.charges for owner in state.attempts.attempts)
    owners = tuple(
        owner
        if index == 0
        else owner.model_copy(
            update={
                "phase": core.AttemptPhase.TERMINAL,
                "closure": core.AttemptClosure(
                    disposition="cancel",
                    authority=core.RequestId(root=f"deadline:{queued[index - 1].decision_id.root}"),
                    admission_id=queued[index - 1].decision_id,
                    requested_at=state.run.now_at,
                ),
            }
        )
        for index, owner in enumerate(state.attempts.attempts)
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=owners)})
    ended = 0
    for request in queued:
        assert isinstance(request, core.AttemptRequest)
        result = core.step(
            _reload(state),
            core.QueueEntryRetired(
                attempt=core.AttemptRef(
                    attempt_id=request.attempt_id, generation=request.generation
                ),
                admission_id=request.decision_id,
            ),
        )
        ended += sum(isinstance(event, core.RunEnded) for event in result.events)
        state = result.state
        assert state.run.receipts == receipts
        assert tuple(owner.charges for owner in state.attempts.attempts) == charges
    assert ended == 1
    assert state.run.status == core.RunStatus.TERMINAL
    assert state.scheduling.queue == state.scheduling.slots == ()


@given(
    count=st.integers(1, 6),
    charge=st.integers(1, 5),
    generation=st.integers(0, 5),
    deadline=st.integers(1, 1000),
)
@example(count=1, charge=1, generation=0, deadline=10)
def test_closing_intake_preserves_accepted_fifo_execution(
    count: int, charge: int, generation: int, deadline: int
) -> None:
    state = _queued_state(count, charge, generation)
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"deadline_at": float(deadline)})}
    )
    # Occupied capacity prevents drain from attempting execution immediately.
    held = core.Slot(
        attempt=core.AttemptRef(attempt_id=core.AttemptId(root="held"), generation=generation),
        admission_id=core.DecisionId(root="held-admission"),
        admitted_at=0.0,
    )
    state = state.model_copy(
        update={"scheduling": state.scheduling.model_copy(update={"slots": (held,)})}
    )
    closed = core.step(_reload(state), core.AdmissionControl(action="drain")).state
    assert closed.scheduling.admission_closed
    assert closed.run.result is None
    assert closed.run.status == core.RunStatus.RUNNING
    assert closed.scheduling.queue == state.scheduling.queue
    assert closed.attempts == state.attempts
    proposed = _queued_state(count + 1, charge, generation)
    newcomer = proposed.scheduling.queue[-1]
    closed = closed.model_copy(
        update={
            "run": closed.run.model_copy(
                update={"receipts": (*closed.run.receipts, proposed.run.receipts[-1])}
            )
        }
    )
    assert isinstance(newcomer, core.AttemptRequest)
    rejected = core.step(_reload(closed), core.AttemptRequested(request=newcomer))
    assert rejected.state.scheduling == closed.scheduling
    assert rejected.requests == ()
    assert any(
        isinstance(event, core.Rejected) and event.code == core.RejectionCode.CLOSED_SCOPE
        for event in rejected.events
    )
    _assert_attempts_signal(
        closed,
        core.SlotReleased(attempt=held.attempt, admission_id=held.admission_id),
        "attempt_admitted",
    )


@given(
    paused=st.booleans(),
    phase=st.sampled_from(tuple(core.RecoveryPhase)),
    deadline=st.integers(1, 1000),
    count=st.integers(1, 6),
)
def test_closed_intake_keeps_pause_recovery_and_deadline_execution_guards(
    *, paused: bool, phase: core.RecoveryPhase, deadline: int, count: int
) -> None:
    state = _queued_state(count, 1, 0)
    state = state.model_copy(
        update={
            "scheduling": state.scheduling.model_copy(update={"admission_closed": True}),
            "run": state.run.model_copy(
                update={
                    "deadline_at": float(deadline),
                    "status": core.RunStatus.PAUSED if paused else core.RunStatus.RUNNING,
                }
            ),
            "intents": state.intents.model_copy(
                update={"recovery": core.RecoveryBarrier(phase=phase)}
            ),
        }
    )
    event = core.ClockAdvanced(now_at=0.0)
    if not paused and phase == core.RecoveryPhase.READY:
        _assert_attempts_signal(state, event, "attempt_admitted")
    else:
        result = core.step(_reload(state), event)
        assert result.requests == result.events == ()
        assert result.state.scheduling == state.scheduling
    expired = state.model_copy(
        update={"run": state.run.model_copy(update={"now_at": float(deadline)})}
    )
    _assert_attempts_signal(expired, core.ClockAdvanced(now_at=float(deadline)), "retire_requested")
