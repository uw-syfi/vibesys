"""Missing target episodes and raw operation IDs grant no inspection ingress."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_recovery_proof_regressions import _digest, _registered_turn


@st.composite
def inspection_facts(draw: st.DrawFn) -> tuple[core.CoreState, core.RequestObserved]:
    """Both query and original carry independent recorded source episodes."""
    generation = draw(st.integers(min_value=0, max_value=20))
    state = core.initial_state()
    scope = core.Scope(owner=core.AttemptId(root="owner"), generation=generation)
    episode = core.DecisionId(root="original-episode")
    spec = core.SessionSpec(
        session_id=core.SessionId(root="session"),
        role_id=core.RoleId(root="worker"),
        policy="reuse",
        lifetime="owner",
        access=core.Access.WRITE_ARTIFACTS,
    )
    source_id = core.RequestId(root="original")
    query_id = core.RequestId(root="query")
    source = core.EnsureSession(
        request_id=source_id,
        scope=scope,
        admission_id=episode,
        deadline_at=100.0,
        spec=spec,
    )
    query = core.InspectRequest(
        request_id=query_id,
        scope=scope,
        deadline_at=100.0,
        admission_id=core.DecisionId(root="query-episode"),
        target=source_id,
    )
    records = tuple(
        core.Intent(
            request_id=identity,
            request=request,
            payload_digest="fixture",
            lifecycle=core.LifecycleClass.QUERY
            if request == query
            else core.LifecycleClass.IDEMPOTENT_WRITE,
            phase=core.IntentPhase.DISPATCHED,
            reconcile_deadline_at=100.0,
        )
        for identity, request in ((source_id, source), (query_id, query))
    )
    state = state.model_copy(update={"intents": core.IntentsState(intents=records)})
    observations = tuple(
        core.Observation(
            event_id=core.EventId(root=identity.root),
            request_id=identity,
            scope=scope,
            admission_id=request.admission_id,
            sequence=1,
            observed_at=1.0,
            status=core.ObservationStatus.UNKNOWN,
        )
        for identity, request in ((source_id, source), (query_id, query))
    )
    return state, core.RequestObserved(
        observation=observations[1],
        target=core.TargetObservation(observation=observations[0]),
    )


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("episode", ["missing", "foreign"])
@given(facts=inspection_facts())
def test_ingress_requires_each_recorded_source_episode(
    facts: tuple[core.CoreState, core.RequestObserved],
    episode: str,
    *,
    nested: bool,
) -> None:
    """Absent admission cannot match an original or an inspection query episode."""
    state, event = facts
    assert event.target is not None
    wrong = None if episode == "missing" else core.DecisionId(root="foreign-episode")
    if nested:
        event = event.model_copy(
            update={
                "target": event.target.model_copy(
                    update={
                        "observation": event.target.observation.model_copy(
                            update={"admission_id": wrong}
                        ),
                    }
                )
            }
        )
    else:
        event = event.model_copy(
            update={"observation": event.observation.model_copy(update={"admission_id": wrong})}
        )
    with pytest.raises(core.ContractError, match="episode"):
        core.step(state, event)
    assert state.intents.intents[0].observation is None
    assert state.intents.intents[1].observation is None


@pytest.mark.parametrize(
    "field", ["exact", "absent_receipt", "feedback", "descriptor", "normalization", "lifecycle"]
)
def test_registered_turn_inspection_requires_accepted_declared_normalized_origin(
    field: str,
) -> None:
    """A matching raw operation ID cannot certify the inspected invocation."""
    state, record = _registered_turn()
    invocation = state.sessions.invocations[0]
    assert record.observation is not None
    query_id = core.RequestId(root="inspection")
    query = core.InspectTurn(
        request_id=query_id,
        scope=record.request.scope,
        deadline_at=100.0,
        invocation=invocation.invocation,
    )
    query_record = core.Intent(
        request_id=query_id,
        request=query,
        payload_digest=_digest(query),
        lifecycle=core.LifecycleClass.QUERY,
        phase=core.IntentPhase.DISPATCHED,
        reconcile_deadline_at=100.0,
    )
    event = core.RequestObserved(
        observation=core.Observation(
            event_id=core.EventId(root="inspection"),
            request_id=query_id,
            scope=query.scope,
            sequence=1,
            observed_at=2.0,
            status=core.ObservationStatus.UNKNOWN,
        ),
        target=core.TargetObservation(observation=record.observation),
    )
    receipt = state.run.receipts[0]
    if field == "feedback":
        receipt = receipt.model_copy(
            update={
                "feedback": receipt.feedback.model_copy(
                    update={
                        "decision_id": core.DecisionId(root="foreign"),
                    }
                )
            }
        )
    elif field == "normalization":
        assert isinstance(receipt.decision, core.Operation)
        decision = receipt.decision.model_copy(update={"normalized_turn": None})
        receipt = receipt.model_copy(
            update={"decision": decision, "payload_digest": _digest(decision)}
        )
    elif field == "lifecycle":
        record = record.model_copy(update={"lifecycle": core.LifecycleClass.IDEMPOTENT_WRITE})
    state = state.model_copy(
        update={
            "registry": () if field == "descriptor" else state.registry,
            "run": state.run.model_copy(
                update={"receipts": () if field == "absent_receipt" else (receipt,)}
            ),
            "intents": state.intents.model_copy(update={"intents": (record, query_record)}),
        }
    )
    if field == "exact":
        # Exact ingress reaches the explicitly unimplemented ledger producer.
        with pytest.raises(core.KernelNotImplementedError, match="_intent_ledger"):
            core.step(state, event)
    else:
        with pytest.raises(core.ContractError, match="invocation"):
            core.step(state, event)
