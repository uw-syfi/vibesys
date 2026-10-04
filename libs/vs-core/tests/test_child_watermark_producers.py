"""Recovery-produced source watermarks compose with kernel run finality."""

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core

from .test_intent_child_guards import pending_intent, recovering_state, reload


def discovered_state(count: int = 1) -> core.CoreState:
    """Each conclusive parent independently discovers the same descendant."""
    parents = []
    for index in range(count):
        parent = pending_intent(f"source:{index}", phase=core.IntentPhase.COMPLETED)
        observation = core.Observation(
            event_id=core.EventId(root=f"parent:{index}"),
            request_id=parent.request_id,
            scope=parent.request.scope,
            sequence=1,
            observed_at=1.0,
            status=core.ObservationStatus.SUCCEEDED,
            resource_id=core.ResourceId(root=f"parent:{index}"),
            accepted=True,
            terminal=True,
            released=True,
            children_complete=True,
            children=(core.ResourceId(root="child"),),
        )
        parents.append(parent.model_copy(update={"observation": observation}))
    return core.step(recovering_state(*parents), core.RecoveryStarted(epoch=1, now_at=1.0)).state


def inspect_child(
    state: core.CoreState,
    source: int,
    sequence: int,
    *,
    query_epoch: int | None = None,
    **facts: object,
) -> tuple[core.RequestObserved, core.IntentsChange]:
    """Feed the exact durable inspection's response to the public recovery leaf."""
    query = next(
        row
        for row in state.intents.intents
        if isinstance(row.request, core.InspectRequest)
        and row.request.resource_id == core.ResourceId(root="child")
        and row.request.target == core.RequestId(root=f"source:{source}")
        and row.request_id.root.startswith(
            f"recovery:child:{state.intents.recovery.epoch if query_epoch is None else query_epoch}:"
        )
    )
    assert isinstance(query.request, core.InspectRequest)
    target = core.Observation.model_validate(
        {
            "event_id": core.EventId(root=f"child:{source}:{sequence}"),
            "request_id": query.request.target,
            "scope": query.request.scope,
            "sequence": sequence,
            "observed_at": 1.0,
            "status": core.ObservationStatus.SUCCEEDED,
            "resource_id": query.request.resource_id,
            "accepted": True,
            "terminal": True,
            "released": True,
            "children_complete": True,
            **facts,
        }
    )
    event = core.RequestObserved(
        observation=core.Observation(
            event_id=core.EventId(root=f"query:{source}:{sequence}"),
            request_id=query.request_id,
            scope=query.request.scope,
            sequence=sequence,
            observed_at=1.0,
            status=core.ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            children_complete=True,
        ),
        target=core.TargetObservation(
            target_resource=query.request.resource_id, observation=target
        ),
    )
    # Intents A is an explicit stub. Its committed query ledger is a boundary
    # input; the real Intents B leaf owns the target watermark and barrier.
    query = query.model_copy(
        update={"phase": core.IntentPhase.COMPLETED, "observation": event.observation}
    )
    ledger = state.intents.model_copy(
        update={
            "intents": tuple(
                query if row.request_id == query.request_id else row
                for row in state.intents.intents
            )
        }
    )
    context = core.IntentsContext(
        run=state.run,
        registry=state.registry,
        attempts=state.attempts,
        sessions=state.sessions,
        evaluation=state.evaluation,
    )
    change = core.recover(ledger, context, event)
    return event, core.IntentsChange(**change.model_dump())


def apply_inspection(
    state: core.CoreState, event: core.RequestObserved, change: core.IntentsChange
) -> core.CoreState:
    """The kernel consumes real recovery output with only the stub peers scripted."""
    frames = [core.TraceFrame(signal=event, change=change)]
    for signal in change.signals:
        assert isinstance(signal, core.RecoveryReady)
        frames.append(
            core.TraceFrame(
                signal=core.ClockAdvanced(
                    now_at=max(state.run.now_at, event.observation.observed_at)
                ),
                change=core.SchedulingChange(state=state.scheduling),
            )
        )
    return core.trace_step(state, event, core.ReducerTrace(frames=tuple(frames))).state


def drain(state: core.CoreState) -> core.Transition:
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": core.RunResultProposal(outcome="cancelled", reason="cleanup"),
                }
            )
        }
    )
    clock = core.ClockAdvanced(now_at=1.0)
    return core.trace_step(
        reload(state),
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


def test_discovered_child_inspection_produces_finality_proof() -> None:
    state = discovered_state()
    lease = state.intents.children[0]
    assert not lease.watermark_history_complete
    assert drain(state).state.run.status == core.RunStatus.CLOSING
    event, change = inspect_child(state, 0, 2)
    assert event.target is not None
    state = apply_inspection(state, event, change)
    lease = state.intents.children[0]
    assert lease.watermark_history_complete
    assert lease.observation_watermarks == (
        core.ChildObservationWatermark(
            source_request=core.RequestId(root="source:0"), observation=event.target.observation
        ),
    )
    assert state.intents.recovery.phase == core.RecoveryPhase.READY
    closed = drain(state)
    assert closed.state.run.status == core.RunStatus.TERMINAL
    assert any(isinstance(event, core.RunEnded) for event in closed.events)


def test_legacy_aggregate_is_not_certified_without_source_inspection() -> None:
    state = discovered_state()
    source = next(
        row for row in state.intents.intents if row.request_id == core.RequestId(root="source:0")
    )
    assert source.observation is not None
    child = state.intents.children[0].model_copy(
        update={
            "observation": source.observation.model_copy(
                update={"resource_id": core.ResourceId(root="child"), "children": ()}
            ),
        }
    )
    state = state.model_copy(
        update={"intents": core.IntentsState(intents=(source,), children=(child,))}
    )
    recovered = core.step(reload(state), core.RecoveryStarted(epoch=2, now_at=1.0))
    assert recovered.state.intents.children == (child,)
    assert recovered.state.intents.recovery.phase == core.RecoveryPhase.RECOVERING
    assert any(
        isinstance(request, core.InspectRequest) and request.resource_id == child.resource_id
        for request in recovered.requests
    )
    assert drain(recovered.state).state.run.status == core.RunStatus.CLOSING


@given(sequence=st.integers(min_value=1, max_value=100), descendants=st.booleans())
def test_new_discovery_source_invalidates_previously_complete_history(
    sequence: int, *, descendants: bool
) -> None:
    state = discovered_state()
    event, change = inspect_child(state, 0, sequence)
    state = apply_inspection(state, event, change)
    assert state.intents.children[0].watermark_history_complete
    source = pending_intent("source:1", phase=core.IntentPhase.COMPLETED)
    assert event.target is not None
    parent = event.target.observation.model_copy(
        update={
            "event_id": core.EventId(root="new-parent"),
            "request_id": source.request_id,
            "resource_id": core.ResourceId(root="new-parent"),
            "children": (core.ResourceId(root="child"),),
        }
    )
    source = source.model_copy(update={"observation": parent})
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={"intents": (*state.intents.intents, source)}
            )
        }
    )
    context = core.IntentsContext(
        run=state.run,
        registry=state.registry,
        attempts=state.attempts,
        sessions=state.sessions,
        evaluation=state.evaluation,
    )
    event = core.RequestObserved(observation=parent)
    change = core.recover(state.intents, context, event)
    state = apply_inspection(state, event, core.IntentsChange(**change.model_dump()))
    lease = state.intents.children[0]
    assert lease.source_requests == (core.RequestId(root="source:0"), source.request_id)
    assert not lease.watermark_history_complete
    assert drain(state).state.run.status == core.RunStatus.CLOSING
    event, change = inspect_child(
        state, 1, 1, children=(core.ResourceId(root="grandchild"),) if descendants else ()
    )
    state = apply_inspection(state, event, change)
    assert state.intents.children[0].watermark_history_complete
    assert drain(state).state.run.status == (
        core.RunStatus.CLOSING if descendants else core.RunStatus.TERMINAL
    )


@given(count=st.integers(min_value=1, max_value=5), inspected=st.integers(min_value=0, max_value=5))
def test_authoritative_coverage_requires_every_discovery_source(count: int, inspected: int) -> None:
    state = discovered_state(count)
    for index in range(min(count, inspected)):
        event, change = inspect_child(state, index, 10 - index)
        state = apply_inspection(state, event, change)
    lease = reload(state).intents.children[0]
    assert lease.watermark_history_complete == (inspected >= count)
    assert len(lease.observation_watermarks) == min(count, inspected)
    assert drain(state).state.run.status == (
        core.RunStatus.TERMINAL if inspected >= count else core.RunStatus.CLOSING
    )


@example(terminal=False, released=False, complete=True, status=core.ObservationStatus.PENDING)
@given(
    terminal=st.booleans(),
    released=st.booleans(),
    complete=st.booleans(),
    status=st.sampled_from(tuple(core.ObservationStatus)),
)
def test_produced_source_coverage_keeps_conservative_release(
    *, terminal: bool, released: bool, complete: bool, status: core.ObservationStatus
) -> None:
    state = discovered_state(2)
    event, change = inspect_child(
        state,
        0,
        20,
        terminal=terminal,
        released=released,
        children_complete=complete,
        status=status,
    )
    state = apply_inspection(state, event, change)
    event, change = inspect_child(state, 1, 2)
    state = apply_inspection(state, event, change)
    conclusive = (
        terminal
        and released
        and complete
        and status not in (core.ObservationStatus.PENDING, core.ObservationStatus.UNKNOWN)
    )
    assert state.intents.children[0].watermark_history_complete
    assert drain(state).state.run.status == (
        core.RunStatus.TERMINAL if conclusive else core.RunStatus.CLOSING
    )


@given(
    sequence=st.integers(min_value=2, max_value=100),
    foreign=st.integers(min_value=1, max_value=100),
)
def test_watermarks_compare_only_with_their_own_source(sequence: int, foreign: int) -> None:
    state = discovered_state(2)
    event, change = inspect_child(
        state, 0, sequence, released=False, terminal=False, status=core.ObservationStatus.PENDING
    )
    state = apply_inspection(state, event, change)
    event, change = inspect_child(state, 1, foreign)
    state = apply_inspection(state, event, change)
    retained = state.intents.children[0]
    event, change = inspect_child(state, 0, sequence - 1)
    assert change.state.children == (retained,)
    event, change = inspect_child(
        state, 0, sequence, released=False, terminal=False, status=core.ObservationStatus.PENDING
    )
    assert change.state.children == (retained,)
    with pytest.raises(core.ContractError, match="conflicting ownership observation sequence"):
        inspect_child(state, 0, sequence)
    event, change = inspect_child(state, 0, sequence + 1)
    state = apply_inspection(state, event, change)
    assert drain(state).state.run.status == core.RunStatus.TERMINAL


@given(
    sequence=st.integers(min_value=1, max_value=100),
    retained=st.booleans(),
    count=st.integers(min_value=1, max_value=4),
)
def test_identical_fresh_inspections_certify_incomplete_history_without_rewriting_facts(
    sequence: int, *, retained: bool, count: int
) -> None:
    state = discovered_state(count)
    for index in range(count):
        event, change = inspect_child(state, index, sequence)
        state = apply_inspection(state, event, change)
    certified = state.intents.children[0]
    incomplete = certified.model_copy(
        update={
            "observation_watermarks": certified.observation_watermarks[:1] if retained else (),
            "watermark_history_complete": False,
        }
    )
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"children": (incomplete,)})}
    )
    for index in range(count):
        event, change = inspect_child(state, index, sequence)
        state = apply_inspection(state, event, change)
    restored = state.intents.children[0]
    assert restored == certified
    event, change = inspect_child(state, 0, sequence)
    assert change.state.children == (certified,)
    assert drain(state).state.run.status == core.RunStatus.TERMINAL


@given(
    phase=st.sampled_from(tuple(core.IntentPhase)),
    terminal=st.booleans(),
    accepted=st.booleans(),
    status=st.sampled_from(tuple(core.ObservationStatus)),
)
def test_only_exact_completed_successful_query_certifies_child_history(
    phase: core.IntentPhase, *, terminal: bool, accepted: bool, status: core.ObservationStatus
) -> None:
    state = discovered_state()
    event, _ = inspect_child(state, 0, 2)
    event = event.model_copy(
        update={
            "observation": event.observation.model_copy(
                update={"terminal": terminal, "accepted": accepted, "status": status}
            )
        }
    )
    records = tuple(
        row.model_copy(update={"phase": phase, "observation": event.observation})
        if row.request_id == event.observation.request_id
        else row
        for row in state.intents.intents
    )
    ledger = state.intents.model_copy(update={"intents": records})
    context = core.IntentsContext(
        run=state.run,
        registry=state.registry,
        attempts=state.attempts,
        sessions=state.sessions,
        evaluation=state.evaluation,
    )
    change = core.recover(ledger, context, event)
    proved = (
        phase == core.IntentPhase.COMPLETED
        and terminal
        and accepted
        and status == core.ObservationStatus.SUCCEEDED
    )
    assert change.state.children[0].watermark_history_complete == proved
    assert bool(change.state.children[0].observation_watermarks) == proved


@given(sequence=st.integers(min_value=2, max_value=100))
def test_foreign_refresh_cannot_erase_migrated_source_sequence_bound(sequence: int) -> None:
    state = discovered_state(2)
    event, change = inspect_child(
        state, 1, sequence, terminal=False, released=False, status=core.ObservationStatus.PENDING
    )
    state = apply_inspection(state, event, change)
    legacy = state.intents.children[0].model_copy(update={"observation_watermarks": ()})
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"children": (legacy,)})}
    )
    event, change = inspect_child(state, 0, 1)
    state = apply_inspection(state, event, change)
    assert state.intents.children[0].observation == legacy.observation
    event, change = inspect_child(state, 1, sequence - 1)
    assert change.state.children == state.intents.children
    assert not change.state.children[0].watermark_history_complete
    with pytest.raises(core.ContractError, match="conflicting ownership observation sequence"):
        inspect_child(state, 1, sequence)
    event, change = inspect_child(state, 1, sequence + 1)
    state = apply_inspection(state, event, change)
    assert state.intents.children[0].watermark_history_complete
    assert drain(state).state.run.status == core.RunStatus.TERMINAL


@given(
    epoch=st.integers(min_value=2, max_value=10), sequence=st.integers(min_value=1, max_value=100)
)
def test_cached_old_epoch_inspection_cannot_recertify_missing_history(
    epoch: int, sequence: int
) -> None:
    state = discovered_state()
    old_event, change = inspect_child(state, 0, sequence)
    state = apply_inspection(state, old_event, change)
    legacy = state.intents.children[0].model_copy(
        update={"observation_watermarks": (), "watermark_history_complete": False}
    )
    state = state.model_copy(
        update={
            "intents": state.intents.model_copy(
                update={"children": (legacy,), "recovery": core.RecoveryBarrier(epoch=epoch - 1)}
            )
        }
    )
    state = core.step(reload(state), core.RecoveryStarted(epoch=epoch, now_at=1.0)).state
    context = core.IntentsContext(
        run=state.run,
        registry=state.registry,
        attempts=state.attempts,
        sessions=state.sessions,
        evaluation=state.evaluation,
    )
    replayed = core.recover(state.intents, context, old_event)
    assert replayed.state.children == (legacy,)
    assert replayed.state.recovery.phase == core.RecoveryPhase.RECOVERING
    state = state.model_copy(update={"intents": replayed.state})
    again = core.recover(state.intents, context, core.RecoveryStarted(epoch=epoch + 1, now_at=1.0))
    assert again.state.recovery.phase == core.RecoveryPhase.RECOVERING
    assert any(
        isinstance(request, core.InspectRequest) and request.resource_id == legacy.resource_id
        for request in again.requests
    )
    # Correlate the real current-epoch query, preserving the same source fact.
    event, change = inspect_child(state, 0, sequence, query_epoch=epoch)
    state = apply_inspection(state, event, change)
    assert state.intents.children[0].watermark_history_complete
    assert drain(state).state.run.status == core.RunStatus.TERMINAL


@pytest.mark.parametrize("identity", ["query", "source"])
@given(duplicates=st.integers(min_value=1, max_value=3))
def test_ambiguous_canonical_source_cannot_produce_child_watermark(
    identity: str, duplicates: int
) -> None:
    state = discovered_state()
    event, change = inspect_child(state, 0, 1)
    assert event.target is not None
    selected = (
        event.observation.request_id if identity == "query" else event.target.observation.request_id
    )
    record = next(row for row in change.state.intents if row.request_id == selected)
    ledger = change.state.model_copy(
        update={
            "children": state.intents.children,
            "intents": (*change.state.intents, *((record,) * duplicates)),
        }
    )
    context = core.IntentsContext(
        run=state.run,
        registry=state.registry,
        attempts=state.attempts,
        sessions=state.sessions,
        evaluation=state.evaluation,
    )
    with pytest.raises(core.ContractError, match="unique canonical source"):
        core.recover(ledger, context, event)
