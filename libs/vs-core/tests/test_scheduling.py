"""Scheduling properties through the public core, with immutable sibling proofs.

Admissions intentionally reach the typed Attempts stub boundary until that
independent slice lands. No test replaces reducers or imports private modules.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter

import vs_core.api as core


def _request(
    index: int, *, charge: int = 1, pools: tuple[core.PoolId, ...] = ()
) -> core.AttemptRequest:
    return core.AttemptRequest(
        decision_id=core.DecisionId(root=f"admission-{index}"),
        attempt_id=core.AttemptId(root=f"attempt-{index}"),
        item_id=core.ItemId(root=f"item-{index}"),
        generation=0,
        admission_charge=charge,
        pools=pools,
    )


def _ref(request: core.AttemptRequest) -> core.AttemptRef:
    return core.AttemptRef(attempt_id=request.attempt_id, generation=request.generation)


def _owner(
    request: core.AttemptRequest,
    *,
    phase: core.AttemptPhase = core.AttemptPhase.QUEUED,
    mode: core.WorkspaceMode = core.WorkspaceMode.ISOLATED_CHILD,
    refunded: int = 0,
) -> core.AttemptView:
    base = core.initial_state().run.facts.baseline
    return core.AttemptView(
        attempt_id=request.attempt_id,
        item_id=request.item_id,
        generation=request.generation,
        phase=phase,
        workspace=core.WorkspacePlan(mode=mode, base=base),
        budget=core.AttemptBudget(admission_charge=request.admission_charge),
        admission_id=request.decision_id if phase != core.AttemptPhase.QUEUED else None,
        charges=(
            core.ChargeReceipt(
                charge_id=core.ChargeId(root=f"charge-{request.attempt_id.root}"),
                kind=core.ChargeKind.ADMISSION,
                charged=request.admission_charge,
                refunded=refunded,
                refund_sources=(core.RequestId(root=f"refund-{request.attempt_id.root}"),)
                if refunded
                else (),
            ),
        ),
    )


def _slot(request: core.AttemptRequest, *, admitted_at: float = 0.0) -> core.Slot:
    return core.Slot(
        attempt=_ref(request),
        admission_id=request.decision_id,
        pools=request.pools,
        admitted_at=admitted_at,
    )


def _state(
    *,
    owners: tuple[core.AttemptView, ...] = (),
    queue: tuple[core.AdmissionRequest, ...] = (),
    slots: tuple[core.Slot, ...] = (),
    paused: bool = False,
    limits: core.Limits | None = None,
) -> core.CoreState:
    state = core.initial_state()
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.PAUSED if paused else core.RunStatus.RUNNING,
                    "limits": limits if limits is not None else core.Limits(max_attempts=20),
                }
            ),
            "attempts": core.AttemptsState(attempts=owners),
            "scheduling": core.SchedulingState(queue=queue, slots=slots),
        }
    )
    for request in queue:
        if isinstance(request, core.AttemptRequest):
            state = _canonical_start(state, request)
    return state


def _step(state: core.CoreState, event: core.CoreEvent) -> core.Transition:
    """Every boundary survives reload and leaves the input untouched."""
    before = state.model_dump_json()
    loaded = core.CoreState.model_validate_json(before)
    wire_event = TypeAdapter(core.CoreEvent).validate_json(
        TypeAdapter(core.CoreEvent).dump_json(event)
    )
    result = core.step(state, event)
    replay = core.step(loaded, wire_event)
    assert result == replay
    assert state.model_dump_json() == before
    assert core.CoreState.model_validate_json(result.state.model_dump_json()) == result.state
    return result


@given(
    times=st.lists(st.integers(0, 100), max_size=30),
    slots=st.integers(1, 4),
)
def test_supplied_clocks_are_monotonic_and_occupancy_is_derived(
    times: list[int], slots: int
) -> None:
    """Translate HostCore supplied-time and slot_seconds invariants (C:329, C:341)."""
    requests = tuple(_request(index) for index in range(slots))
    state = _state(
        owners=tuple(_owner(request, phase=core.AttemptPhase.ACTIVE) for request in requests),
        slots=tuple(_slot(request) for request in requests),
        limits=core.Limits(max_attempts=20, max_parallel=slots),
    )
    now = 0
    for increment in times:
        supplied = now + increment
        now = supplied
        result = _step(state, core.ClockAdvanced(now_at=float(supplied)))
        assert result.requests == result.events == ()
        state = result.state
        view = core.project(state)
        assert view.run.now_at == now
        assert view.scheduling.active_slot_seconds == now * slots
        assert view.scheduling.slot_seconds == 0
        assert state.attempts.attempts == tuple(
            _owner(request, phase=core.AttemptPhase.ACTIVE) for request in requests
        )


@given(
    events=st.lists(
        st.tuples(
            st.sampled_from(["clock", "end", "release", "ready"]),
            st.integers(0, 100),
            st.booleans(),
            st.booleans(),
        ),
        max_size=40,
    )
)
def test_duplicate_reordered_and_stale_episode_events_settle_occupancy_once(
    events: list[tuple[str, int, bool, bool]],
) -> None:
    """First charge-end wins; release credits one interval, old episodes are inert."""
    request = _request(0)
    state = _state(
        owners=(_owner(request, phase=core.AttemptPhase.ACTIVE),), slots=(_slot(request),)
    )
    held = True
    ended: int | None = None
    released = 0
    now = 0
    for kind, at, stale_episode, stale_generation in events:
        target = _ref(request).model_copy(update={"generation": int(stale_generation)})
        admission = core.DecisionId(root="old") if stale_episode else request.decision_id
        if kind == "clock":
            event: core.CoreEvent = core.ClockAdvanced(now_at=float(at))
            if at < now:
                with pytest.raises(core.ContractValidationError, match=r"time|clock|now_at"):
                    _step(state, event)
                continue
            now = at
        elif kind == "end":
            event = core.SlotChargeEnded(attempt=target, admission_id=admission, ended_at=float(at))
            now = max(now, at)
            if held and not stale_episode and not stale_generation and ended is None:
                ended = at
        elif kind == "release":
            event = core.SlotReleased(attempt=target, admission_id=admission)
            if held and not stale_episode and not stale_generation:
                released = now if ended is None else ended
                held = False
        else:
            event = core.AttemptReady(attempt=target, admission_id=admission)
        result = _step(state, event)
        assert result.requests == ()
        assert sum(isinstance(item, core.AttemptReady) for item in result.events) <= 1
        state = result.state
        view = core.project(state).scheduling
        assert len(view.slots) == int(held)
        assert view.slot_seconds == (ended if held and ended is not None else released)
        assert view.active_slot_seconds == (now if held and ended is None else 0)
        assert state.scheduling.released_slot_seconds == released
        assert view.charged == 1
        assert view.refunded == 0


@given(
    count=st.integers(1, 8),
    operations=st.lists(st.tuples(st.integers(0, 7), st.booleans()), max_size=30),
)
def test_queued_duplicates_and_retirement_preserve_fifo_and_charges(
    count: int, operations: list[tuple[int, bool]]
) -> None:
    """Translate HostCore queue removal C:429 and duplicate admission C:403."""
    requests = tuple(_request(index) for index in range(count))
    state = _state(
        owners=tuple(_owner(request) for request in requests), queue=requests, paused=True
    )
    expected = list(requests)
    for selected, stale in operations:
        request = requests[selected % count]
        target = _ref(request).model_copy(update={"generation": int(stale)})
        event = core.QueueEntryRetired(attempt=target)
        if not stale:
            expected = [item for item in expected if item.attempt_id != target.attempt_id]
        result = _step(state, event)
        assert result.requests == result.events == ()
        state = result.state
        assert state.scheduling.queue == tuple(expected)
        assert state.scheduling.slots == ()
        assert core.project(state).scheduling.charged == count
        assert core.project(state).scheduling.refunded == 0


def test_duplicate_queued_start_does_not_register_or_charge_again() -> None:
    request = _request(0)
    state = _state(owners=(_owner(request),), queue=(request,), paused=True)
    for _ in range(3):
        result = _step(state, core.AttemptRequested(request=request))
        state = result.state
        assert result.requests == result.events == ()
        assert state.scheduling.queue == (request,)
        assert core.project(state).scheduling.charged == 1


def test_closure_stops_charging_but_holds_conflicting_capacity_until_release() -> None:
    """Section 13 Slot ordering, cleanup alone never gives B A's slot."""
    first, second = _request(0), _request(1)
    state = _state(
        owners=(
            _owner(first, phase=core.AttemptPhase.CLOSING),
            _owner(second),
        ),
        queue=(second,),
        slots=(_slot(first),),
    )
    result = _step(
        state,
        core.SlotChargeEnded(attempt=_ref(first), admission_id=first.decision_id, ended_at=5.0),
    )
    state = result.state
    assert result.requests == ()
    assert state.scheduling.queue == (second,)
    assert len(state.scheduling.slots) == 1
    state = _step(state, core.ClockAdvanced(now_at=30.0)).state
    assert core.project(state).scheduling.slot_seconds == 5
    assert core.project(state).scheduling.active_slot_seconds == 0
    assert state.scheduling.queue == (second,)


def test_delayed_old_release_cannot_free_reopened_episode() -> None:
    request = _request(0)
    reopened = _slot(request, admitted_at=10.0).model_copy(
        update={"admission_id": core.DecisionId(root="reopened")}
    )
    state = _state(owners=(_owner(request, phase=core.AttemptPhase.ACTIVE),), slots=(reopened,))
    for event in (
        core.AttemptReady(attempt=_ref(request), admission_id=request.decision_id),
        core.SlotChargeEnded(
            attempt=_ref(request), admission_id=request.decision_id, ended_at=20.0
        ),
        core.SlotReleased(attempt=_ref(request), admission_id=request.decision_id),
    ):
        result = _step(state, event)
        state = result.state
        assert result.requests == result.events == ()
        assert state.scheduling.slots == (reopened,)
        assert state.scheduling.released_slot_seconds == 0
    assert core.project(state).scheduling.active_slot_seconds == 10


@given(
    charged=st.integers(0, 20),
    refunded=st.integers(0, 20),
    budget=st.integers(0, 20),
)
def test_admission_usage_and_refunds_have_only_receipt_authority(
    charged: int, refunded: int, budget: int
) -> None:
    """K4: queue movement and telemetry never rewrite authoritative currency."""
    refunded = min(charged, refunded)
    request = _request(0, charge=charged)
    owner = _owner(request, refunded=refunded)
    unrelated = core.ChargeReceipt(
        charge_id=core.ChargeId(root="paid"), kind=core.ChargeKind.ATTEMPT, charged=99
    )
    owner = owner.model_copy(update={"charges": (*owner.charges, unrelated)})
    state = _state(
        owners=(owner,), queue=(request,), paused=True, limits=core.Limits(max_attempts=budget)
    )
    for event in (
        core.ClockAdvanced(now_at=15.0),
        core.QueueEntryRetired(attempt=_ref(request)),
        core.QueueEntryRetired(attempt=_ref(request)),
    ):
        state = _step(state, event).state
        view = core.project(state).scheduling
        assert (view.charged, view.refunded) == (charged, refunded)
        assert 0 <= view.refunded <= view.charged
        assert state.attempts.attempts == (owner,)
    persisted = state.scheduling.model_dump()
    assert "charged" not in persisted
    assert "spent" not in persisted
    assert "refunded" not in persisted


@given(now=st.integers(1, 100), previous=st.integers(0, 99))
def test_explicit_backward_clock_is_rejected_without_mutation(now: int, previous: int) -> None:
    """Translate HostCore's explicit backward-time rejection, C:517."""
    state = _state()
    state = state.model_copy(update={"run": state.run.model_copy(update={"now_at": float(now)})})
    before = state.model_dump_json()
    with pytest.raises(core.ContractValidationError, match=r"time|clock|now_at"):
        core.step(state, core.ClockAdvanced(now_at=float(min(previous, now - 1))))
    assert state.model_dump_json() == before


def _canonical_start(state: core.CoreState, request: core.AttemptRequest) -> core.CoreState:
    owner = next(item for item in state.attempts.attempts if item.attempt_id == request.attempt_id)
    decision = core.StartAttempt(
        decision_id=request.decision_id,
        scope=core.Scope(owner=state.run.run_id, generation=request.generation),
        attempt_id=request.attempt_id,
        item_id=request.item_id,
        workspace=owner.workspace,
        budget=owner.budget,
    )
    receipt = core.DecisionReceipt(
        decision_id=request.decision_id,
        decision=decision,
        payload_digest="fixture-canonical-start",
        feedback=core.Accepted(decision_id=request.decision_id),
    )
    receipts = tuple(item for item in state.run.receipts if item.decision_id != receipt.decision_id)
    return state.model_copy(
        update={"run": state.run.model_copy(update={"receipts": (*receipts, receipt)})}
    )


def _assert_attempts_boundary(state: core.CoreState, event: core.CoreEvent, kind: str) -> None:
    """Prove admission at a typed sibling boundary or in the composed result.

    Frozen sibling stubs may stop propagation before acquisition is available.
    As they land, check the resulting FIFO lease instead of requiring a stub.
    """
    before = state.model_dump_json()
    loaded = core.CoreState.model_validate_json(before)
    failure: core.KernelNotImplementedError | None = None
    result: core.Transition | None = None
    try:
        result = core.step(state, event)
    except core.KernelNotImplementedError as error:
        failure = error
    if failure is not None:
        assert failure.area != core.Area.SCHEDULING
        if failure.area == core.Area.ATTEMPTS:
            assert failure.event_kind == kind
        with pytest.raises(core.KernelNotImplementedError) as replayed:
            core.step(loaded, event)
        assert replayed.value.area == failure.area
        assert replayed.value.event_kind == failure.event_kind
    else:
        assert result is not None
        assert result == core.step(loaded, event)
        head = state.scheduling.queue[0]
        target = _ref(head) if isinstance(head, core.AttemptRequest) else head.attempt
        slot = next(item for item in result.state.scheduling.slots if item.attempt == target)
        assert slot.admission_id == head.decision_id
        assert slot.admitted_at == result.state.run.now_at
        assert head not in result.state.scheduling.queue
        assert (
            core.project(result.state).scheduling.charged == core.project(state).scheduling.charged
        )
    assert state.model_dump_json() == before


def test_freed_slot_selects_fifo_head_at_real_attempts_boundary() -> None:
    """Translate HostCore FIFO C:494 and test_freed_slots_start_the_queue_head."""
    first, head, tail = (_request(index) for index in range(3))
    state = _state(
        owners=(
            _owner(first, phase=core.AttemptPhase.ACTIVE),
            _owner(head),
            _owner(tail),
        ),
        slots=(_slot(first),),
        queue=(head, tail),
    )
    # Only the head has canonical admission proof. Skipping it would fail in the
    # kernel's canonical-decision guard before reaching the Attempts boundary.
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": tuple(
                        receipt
                        for receipt in state.run.receipts
                        if receipt.decision_id == head.decision_id
                    )
                }
            )
        }
    )
    _assert_attempts_boundary(
        state,
        core.SlotReleased(attempt=_ref(first), admission_id=first.decision_id),
        "attempt_admitted",
    )


@pytest.mark.parametrize("gate", ["paused", "recovery", "capacity", "pool", "exclusive"])
def test_fifo_head_cannot_be_bypassed_when_any_admission_gate_is_closed(gate: str) -> None:
    pool = core.PoolId(root="exclusive-pool")
    running = _request(0, pools=(pool,))
    head = _request(1, pools=(pool,) if gate == "pool" else ())
    tail = _request(2)
    mode = (
        core.WorkspaceMode.EXCLUSIVE_ROOT
        if gate == "exclusive"
        else core.WorkspaceMode.ISOLATED_CHILD
    )
    state = _state(
        owners=(
            _owner(running, phase=core.AttemptPhase.CLOSING, mode=mode),
            _owner(head, mode=mode),
            _owner(tail),
        ),
        queue=(head, tail),
        slots=(_slot(running),),
        paused=gate == "paused",
        limits=core.Limits(max_attempts=20, max_parallel=1 if gate == "capacity" else 3),
    )
    if gate == "recovery":
        state = state.model_copy(
            update={
                "intents": state.intents.model_copy(
                    update={"recovery": core.RecoveryBarrier(phase=core.RecoveryPhase.BLOCKED)}
                )
            }
        )
    result = _step(state, core.ClockAdvanced(now_at=25.0))
    assert result.requests == result.events == ()
    assert result.state.scheduling.queue == (head, tail)
    assert result.state.scheduling.slots == (_slot(running),)
    assert result.state.attempts == state.attempts


def test_already_paid_queue_head_admits_with_zero_remaining_admission_budget() -> None:
    """Registration charged queued work; later capacity admission is free, K4."""
    request = _request(0)
    state = _canonical_start(
        _state(owners=(_owner(request),), queue=(request,), limits=core.Limits(max_attempts=1)),
        request,
    )
    assert core.project(state).scheduling.available_tokens == 0
    _assert_attempts_boundary(state, core.ClockAdvanced(now_at=5.0), "attempt_admitted")


@pytest.mark.parametrize("mode", ["drain", "cancel"])
def test_stop_closes_admission_and_duplicate_stop_has_one_terminal_receipt(mode: str) -> None:
    """Translate HostCore no starts after stop and exactly one EndSearch."""
    state = _state()
    decision = core.Stop(
        decision_id=core.DecisionId(root="stop"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        mode=mode,
        result=core.RunResultProposal(outcome="cancelled", reason="requested"),
    )
    result = _step(
        state, core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )
    state = result.state
    assert state.run.status == core.RunStatus.TERMINAL
    assert state.scheduling.admission_closed
    assert result.requests == ()
    assert sum(isinstance(event, core.RunEnded) for event in result.events) == 1
    siblings = (state.attempts, state.sessions, state.evaluation, state.settlement, state.intents)
    for _ in range(3):
        result = _step(
            state, core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
        )
        state = result.state
        assert result.requests == ()
        assert not any(isinstance(event, core.RunEnded) for event in result.events)
        assert len(state.run.receipts) == 1
        assert (
            state.attempts,
            state.sessions,
            state.evaluation,
            state.settlement,
            state.intents,
        ) == siblings


def test_parked_reentry_is_fifo_and_does_not_create_an_admission_charge() -> None:
    first, parked = _request(0), _request(1)
    owner = _owner(parked, phase=core.AttemptPhase.PARKED)
    reopen = core.AttemptReopenRequest(
        decision_id=core.DecisionId(root="reopen"),
        request_id=core.RequestId(root="reopen-operation"),
        attempt=_ref(parked),
    )
    state = _state(owners=(_owner(first), owner), queue=(first,), paused=True)
    for _ in range(3):
        result = _step(state, core.AttemptReopenRequested(request=reopen))
        state = result.state
        assert result.requests == result.events == ()
        assert state.scheduling.queue == (first, reopen)
        assert state.scheduling.slots == ()
        assert core.project(state).scheduling.charged == 2
        assert core.project(state).scheduling.refunded == 0
        assert state.attempts.attempts == (_owner(first), owner)


def test_drain_continues_already_registered_queue_at_attempts_boundary() -> None:
    """Legacy _finishing permits FIFO starts; it only rejects new admissions."""
    request = _request(0)
    state = _state(owners=(_owner(request),), queue=(request,))
    stop = core.Stop(
        decision_id=core.DecisionId(root="stop"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        mode="drain",
        result=core.RunResultProposal(outcome="success", reason="finish accepted work"),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": stop.result,
                    "receipts": (
                        *state.run.receipts,
                        core.DecisionReceipt(
                            decision_id=stop.decision_id,
                            decision=stop,
                            payload_digest="canonical-stop",
                            feedback=core.Accepted(decision_id=stop.decision_id),
                        ),
                    ),
                }
            ),
            "scheduling": state.scheduling.model_copy(update={"admission_closed": True}),
        }
    )
    _assert_attempts_boundary(state, core.ClockAdvanced(now_at=10.0), "attempt_admitted")


def test_old_queued_retirement_cannot_delete_reentry_episode() -> None:
    request = _request(0)
    reopen = core.AttemptReopenRequest(
        decision_id=core.DecisionId(root="reentry"),
        request_id=core.RequestId(root="reentry-operation"),
        attempt=_ref(request),
    )
    owner = _owner(request, phase=core.AttemptPhase.PARKED).model_copy(
        update={
            "closure": core.AttemptClosure(
                disposition="park",
                requested_at=5.0,
                authority=core.RequestId(root="old-park"),
                admission_id=request.decision_id,
            )
        }
    )
    state = _state(owners=(owner,), queue=(reopen,), paused=True)
    for _ in range(3):
        result = _step(state, core.QueueEntryRetired(attempt=_ref(request)))
        state = result.state
        assert result.requests == result.events == ()
        assert state.scheduling.queue == (reopen,)
        assert state.scheduling.slots == ()
    current = owner.model_copy(
        update={
            "phase": core.AttemptPhase.CLOSING,
            "closure": owner.closure.model_copy(update={"admission_id": reopen.decision_id}),
        }
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(current,))})
    result = _step(state, core.QueueEntryRetired(attempt=_ref(request)))
    assert result.state.scheduling.queue == ()
    assert result.state.scheduling.slots == ()
    assert core.project(result.state).scheduling.charged == 1


@given(admitted=st.integers(1, 100), earlier=st.integers(0, 99))
def test_charge_end_cannot_precede_matching_episode_admission(admitted: int, earlier: int) -> None:
    request = _request(0)
    state = _state(
        owners=(_owner(request, phase=core.AttemptPhase.ACTIVE),),
        slots=(_slot(request, admitted_at=float(admitted)),),
    )
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"now_at": float(admitted)})}
    )
    before = state.model_dump_json()
    with pytest.raises(core.ContractValidationError, match="ended_at"):
        core.step(
            state,
            core.SlotChargeEnded(
                attempt=_ref(request),
                admission_id=request.decision_id,
                ended_at=float(min(earlier, admitted - 1)),
            ),
        )
    assert state.model_dump_json() == before


def test_first_terminal_stop_result_survives_a_distinct_later_stop() -> None:
    state = _state()
    first = core.Stop(
        decision_id=core.DecisionId(root="first-stop"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        mode="drain",
        result=core.RunResultProposal(outcome="cancelled", reason="finished"),
    )
    state = _step(
        state, core.DecisionSubmitted(decision=first, expected_revision=state.revision)
    ).state
    later = first.model_copy(
        update={
            "decision_id": core.DecisionId(root="later-stop"),
            "mode": "cancel",
            "result": core.RunResultProposal(outcome="cancelled", reason="too late"),
        }
    )
    result = _step(state, core.DecisionSubmitted(decision=later, expected_revision=state.revision))
    assert result.state.run.result == first.result
    assert result.state.run.status == core.RunStatus.TERMINAL
    assert result.state.scheduling == state.scheduling
    assert result.requests == ()
    assert not any(isinstance(event, core.RunEnded) for event in result.events)
    assert any(isinstance(event, core.Rejected) for event in result.events)


def test_first_stop_during_cleanup_keeps_its_result_and_capacity() -> None:
    request = _request(0)
    state = _state(
        owners=(_owner(request, phase=core.AttemptPhase.CLOSING),), slots=(_slot(request),)
    )
    first = core.Stop(
        decision_id=core.DecisionId(root="first-stop"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        mode="drain",
        result=core.RunResultProposal(outcome="cancelled", reason="first result"),
    )
    result = _step(state, core.DecisionSubmitted(decision=first, expected_revision=state.revision))
    state = result.state
    assert state.run.status == core.RunStatus.CLOSING
    assert state.scheduling.admission_closed
    assert state.scheduling.slots == (_slot(request),)
    assert result.requests == ()
    later = first.model_copy(
        update={
            "decision_id": core.DecisionId(root="second-stop"),
            "mode": "cancel",
            "result": core.RunResultProposal(outcome="cancelled", reason="second result"),
        }
    )
    result = _step(state, core.DecisionSubmitted(decision=later, expected_revision=state.revision))
    assert result.state.run.result == first.result
    assert result.state.scheduling == state.scheduling
    assert result.requests == ()
    assert any(isinstance(event, core.Rejected) for event in result.events)


@given(order=st.lists(st.integers(0, 5), min_size=1, max_size=25))
def test_exhausted_receipt_budget_rejects_reordered_duplicate_starts_once(order: list[int]) -> None:
    """HostCore admission budget C:408 is bounded before registration requests."""
    occupied = _request(99)
    state = _state(
        owners=(_owner(occupied, phase=core.AttemptPhase.ACTIVE),),
        slots=(_slot(occupied),),
        limits=core.Limits(max_attempts=1, max_parallel=3),
    )
    seen: set[int] = set()
    for index in order:
        request = _request(index)
        owner = _owner(request)
        decision = core.StartAttempt(
            decision_id=request.decision_id,
            scope=core.Scope(owner=state.run.run_id, generation=request.generation),
            attempt_id=request.attempt_id,
            item_id=request.item_id,
            workspace=owner.workspace,
            budget=owner.budget,
        )
        result = _step(
            state, core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
        )
        state = result.state
        assert result.requests == ()
        if index not in seen:
            assert any(
                isinstance(event, core.Rejected) and event.code == core.RejectionCode.BUDGET
                for event in result.events
            )
        else:
            assert result.events == ()
        seen.add(index)
        assert state.scheduling.queue == ()
        assert state.scheduling.slots == (_slot(occupied),)
        assert core.project(state).scheduling.charged == 1
        assert core.project(state).scheduling.refunded == 0
        ids = [receipt.decision_id for receipt in state.run.receipts]
        assert len(ids) == len(set(ids))


def test_host_stop_without_registered_result_closes_admission_without_inventing_result() -> None:
    """RunControl has no result proposal in the frozen scheduling context."""
    state = _state()
    result = _step(
        state,
        core.RunControlEvent(
            control=core.ControlInput(control_id=core.ControlId(root="host-stop"), action="stop"),
            now_at=10.0,
        ),
    )
    assert result.state.scheduling.admission_closed
    assert result.state.run.status == core.RunStatus.CLOSING
    assert result.state.run.result is None
    assert result.requests == ()
    assert not any(isinstance(event, core.RunEnded) for event in result.events)
    assert result.state.attempts == state.attempts
    assert result.state.sessions == state.sessions
    assert result.state.evaluation == state.evaluation
    assert result.state.settlement == state.settlement
    assert result.state.intents == state.intents


@pytest.mark.parametrize(
    "missing",
    ["owner", "receipt", "charge", "historical", "closing", "active", "parked", "closure"],
)
def test_persisted_queue_head_needs_registration_and_live_charge_proof(missing: str) -> None:
    head, tail = _request(0), _request(1)
    owner = _owner(head)
    state = _state(owners=(owner, _owner(tail)), queue=(head, tail))
    if missing == "owner":
        owners = (_owner(tail),)
    elif missing == "receipt":
        owners = state.attempts.attempts
        state = state.model_copy(
            update={"run": state.run.model_copy(update={"receipts": state.run.receipts[1:]})}
        )
    elif missing in {"charge", "historical"}:
        charges = ()
        if missing == "historical":
            charges = (
                owner.charges[0].model_copy(
                    update={
                        "historical_proof": core.ArtifactRef(
                            artifact_id=core.ArtifactId(root="legacy-accounting"), digest="legacy"
                        )
                    }
                ),
            )
        owners = (owner.model_copy(update={"charges": charges}), _owner(tail))
    elif missing == "closure":
        owners = (
            owner.model_copy(
                update={
                    "closure": core.AttemptClosure(
                        disposition="cancel",
                        requested_at=0.0,
                        authority=core.RequestId(root="retire"),
                        admission_id=head.decision_id,
                    )
                }
            ),
            _owner(tail),
        )
    else:
        phase = {
            "closing": core.AttemptPhase.CLOSING,
            "active": core.AttemptPhase.ACTIVE,
            "parked": core.AttemptPhase.PARKED,
        }[missing]
        owners = (owner.model_copy(update={"phase": phase}), _owner(tail))
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=owners)})
    result = _step(state, core.ClockAdvanced(now_at=10.0))
    assert result.requests == ()
    assert result.state.scheduling.queue == (head, tail)
    assert result.state.scheduling.slots == ()
    assert result.state.attempts == state.attempts


def test_registered_queued_owner_cannot_claim_another_start_authority() -> None:
    original = _request(0)
    state = _state(owners=(_owner(original),), queue=(original,), paused=True)
    state = state.model_copy(update={"scheduling": core.SchedulingState()})
    changed = original.model_copy(update={"decision_id": core.DecisionId(root="other-start")})
    state = _canonical_start(state, changed)
    result = _step(state, core.AttemptRequested(request=changed))
    assert result.requests == ()
    assert result.state.scheduling == state.scheduling
    assert result.state.attempts == state.attempts
    assert any(
        isinstance(event, core.Rejected) and event.code == core.RejectionCode.OWNERSHIP
        for event in result.events
    )
    assert core.project(result.state).scheduling.charged == 1


def test_duplicate_occupied_start_cannot_change_its_pool_payload() -> None:
    original = _request(0)
    state = _canonical_start(
        _state(
            owners=(_owner(original, phase=core.AttemptPhase.ACTIVE),), slots=(_slot(original),)
        ),
        original,
    )
    changed = original.model_copy(update={"pools": (core.PoolId(root="new-pool"),)})
    result = _step(state, core.AttemptRequested(request=changed))
    assert result.requests == ()
    assert result.state.scheduling == state.scheduling
    assert result.state.attempts == state.attempts
    assert any(isinstance(event, core.Rejected) for event in result.events)


@pytest.mark.parametrize(
    "mode", [core.WorkspaceMode.ISOLATED_CHILD, core.WorkspaceMode.READ_ONLY_REVISION]
)
@pytest.mark.parametrize("root_first", [True, False])
def test_root_mutation_and_independent_workspace_leases_can_share_capacity(
    mode: core.WorkspaceMode, *, root_first: bool
) -> None:
    running, queued = _request(0), _request(1)
    held_mode, next_mode = (
        (core.WorkspaceMode.EXCLUSIVE_ROOT, mode)
        if root_first
        else (mode, core.WorkspaceMode.EXCLUSIVE_ROOT)
    )
    state = _state(
        owners=(
            _owner(running, phase=core.AttemptPhase.ACTIVE, mode=held_mode),
            _owner(queued, mode=next_mode),
        ),
        slots=(_slot(running),),
        queue=(queued,),
        limits=core.Limits(max_attempts=2, max_parallel=2),
    )
    _assert_attempts_boundary(state, core.ClockAdvanced(now_at=10.0), "attempt_admitted")


@given(
    charged=st.integers(0, 10),
    refund=st.integers(0, 10),
    budget=st.integers(0, 10),
    new_cost=st.integers(0, 4),
)
def test_updated_refund_receipts_authorize_only_the_restored_admission_budget(
    charged: int, refund: int, budget: int, new_cost: int
) -> None:
    refunded = min(charged, refund)
    previous = _request(99, charge=charged)
    owner = _owner(previous, phase=core.AttemptPhase.TERMINAL, refunded=refunded)
    owner = owner.model_copy(
        update={
            "charges": (
                *owner.charges,
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root="paid"), kind=core.ChargeKind.ATTEMPT, charged=99
                ),
                core.ChargeReceipt(
                    charge_id=core.ChargeId(root="turn"), kind=core.ChargeKind.TURN, charged=99
                ),
            )
        }
    )
    state = _state(owners=(owner,), limits=core.Limits(max_attempts=budget))
    request = _request(0, charge=new_cost)
    candidate = _owner(request)
    decision = core.StartAttempt(
        decision_id=request.decision_id,
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=request.attempt_id,
        item_id=request.item_id,
        workspace=candidate.workspace,
        budget=candidate.budget,
    )
    result = _step(
        state, core.DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )
    feedback = next(
        item for item in result.events if isinstance(item, core.Accepted | core.Rejected)
    )
    if new_cost > max(0, budget - charged + refunded):
        assert isinstance(feedback, core.Rejected)
        assert feedback.code == core.RejectionCode.BUDGET
        assert result.requests == ()
        assert result.state.scheduling == state.scheduling
    elif isinstance(feedback, core.Rejected):
        assert feedback.code == core.RejectionCode.NOT_IMPLEMENTED_IN_KERNEL
        assert feedback.path[0] != "scheduling"
    else:
        assert isinstance(feedback, core.Accepted)
        assert core.project(result.state).scheduling.charged == charged + new_cost
        assert core.project(result.state).scheduling.refunded == refunded
    assert result.state.attempts.attempts[0] == owner


@pytest.mark.parametrize("proof", ["admission", "closure", "receipt"])
def test_replaying_old_reentry_after_release_never_creates_another_episode(proof: str) -> None:
    request = _request(0)
    reopen = core.AttemptReopenRequest(
        decision_id=core.DecisionId(root="old-reentry"),
        request_id=core.RequestId(root="old-reentry-operation"),
        attempt=_ref(request),
    )
    owner = _owner(request, phase=core.AttemptPhase.PARKED)
    if proof == "admission":
        owner = owner.model_copy(update={"admission_id": reopen.decision_id})
    else:
        owner = owner.model_copy(
            update={
                "closure": core.AttemptClosure(
                    disposition="park",
                    requested_at=10.0,
                    authority=core.RequestId(root="park"),
                    admission_id=reopen.decision_id
                    if proof == "closure"
                    else core.DecisionId(root="newer-reentry"),
                )
            }
        )
    state = _state(owners=(owner,))
    if proof == "receipt":
        receipt = core.DecisionReceipt(
            decision_id=reopen.decision_id,
            payload_digest="completed-historical-reentry",
            feedback=core.Accepted(decision_id=reopen.decision_id),
            completion=core.CompletionStatus.SUCCEEDED,
        )
        state = state.model_copy(
            update={"run": state.run.model_copy(update={"receipts": (receipt,)})}
        )
    for _ in range(3):
        result = _step(state, core.AttemptReopenRequested(request=reopen))
        state = result.state
        assert result.requests == result.events == ()
        assert state.scheduling.queue == ()
        assert state.scheduling.slots == ()
        assert core.project(state).scheduling.charged == 1
        assert core.project(state).scheduling.refunded == 0
        assert state.attempts.attempts == (owner,)


@pytest.mark.parametrize("episode", ["initial", "reentry"])
def test_exact_current_ready_is_forwarded_without_charging_or_capacity_changes(
    episode: str,
) -> None:
    request = _request(0)
    admission = request.decision_id if episode == "initial" else core.DecisionId(root="reentry")
    owner = _owner(request, phase=core.AttemptPhase.ACTIVE).model_copy(
        update={"admission_id": admission}
    )
    slot = _slot(request).model_copy(update={"admission_id": admission})
    state = _state(owners=(owner,), slots=(slot,))
    ready = core.AttemptReady(attempt=_ref(request), admission_id=admission)
    for _ in range(3):
        result = _step(state, ready)
        state = result.state
        assert result.events == (ready,)
        assert result.requests == ()
        assert state.scheduling.slots == (slot,)
        assert state.attempts.attempts == (owner,)
        assert core.project(state).scheduling.charged == 1
        assert core.project(state).scheduling.refunded == 0


@pytest.mark.parametrize(
    "proof",
    [
        "missing-owner",
        "acquiring",
        "closing",
        "parked",
        "wrong-admission",
        "closure",
        "released",
        "charge-ended",
    ],
)
def test_readiness_needs_current_active_episode_proof(proof: str) -> None:
    request = _request(0)
    owner = _owner(request, phase=core.AttemptPhase.ACTIVE)
    slot = _slot(request)
    if proof in {"acquiring", "closing", "parked"}:
        owner = owner.model_copy(update={"phase": core.AttemptPhase(proof)})
    elif proof == "wrong-admission":
        owner = owner.model_copy(update={"admission_id": core.DecisionId(root="other-episode")})
    elif proof == "closure":
        owner = owner.model_copy(
            update={
                "closure": core.AttemptClosure(
                    disposition="cancel",
                    requested_at=0.0,
                    authority=core.RequestId(root="closing"),
                    admission_id=request.decision_id,
                )
            }
        )
    elif proof == "charge-ended":
        slot = slot.model_copy(update={"charge_ended_at": 0.0})
    state = _state(
        owners=() if proof == "missing-owner" else (owner,),
        slots=() if proof == "released" else (slot,),
    )
    ready = core.AttemptReady(attempt=_ref(request), admission_id=request.decision_id)
    result = _step(state, ready)
    assert result.events == result.requests == ()
    assert result.state.scheduling == state.scheduling
    assert result.state.attempts == state.attempts
