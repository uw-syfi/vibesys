"""Registered SESSION_TURN origins supply the same exact attempt authority."""

import json
from hashlib import sha256
from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel

import vs_core.api as core

from .test_attempt_invocation_proofs import context, invocation_fixture


class TurnOutcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class RegisteredTurn(core.OperationRequest):
    kind: Literal["attempt-proof.turn"] = "attempt-proof.turn"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = TurnOutcome
    turn: core.TurnSpec


def normalized_turn(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, RegisteredTurn)
    return request.turn


def turn_registry() -> core.OperationRegistry:
    return core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=core.OperationDescriptor(
                    kind="attempt-proof.turn",
                    request_schema=core.SchemaRef(name="registered-turn", version=1),
                    outcome_schema=core.SchemaRef(name="turn-outcome", version=1),
                    lifecycle=core.LifecycleClass.SESSION_TURN,
                    inspect=True,
                    cancel=True,
                    watch=True,
                ),
                request_model=RegisteredTurn,
                outcome_model=TurnOutcome,
                normalize_turn=normalized_turn,
            ),
        )
    )


def registered_state(
    receipt_kind: str,
    episode: str,
    correlation: str,
    lifecycle: tuple[core.SessionPhase, core.ObservationStatus],
) -> tuple[core.CoreState, core.OperationRegistry]:
    state = invocation_fixture(
        observed_episode=episode,
        correlation=correlation,
        lifecycle=lifecycle,
    )
    invocation = state.sessions.invocations[0]
    codec = turn_registry()
    payload = RegisteredTurn(turn=invocation.turn)
    decision_id = core.DecisionId(root="registered")
    request_id = core.RequestId(root="turn")
    operation_id = core.OperationId(root="operation:registered")
    request = core.ExecuteRegisteredOperation(
        request_id=request_id,
        decision_id=decision_id,
        operation_id=operation_id,
        scope=invocation.scope,
        admission_id=core.DecisionId(root="current"),
        deadline_at=1000,
        operation=codec.encode(payload),
        retry_limit=0,
    )
    canonical_payload = (
        payload
        if receipt_kind != "mismatched"
        else RegisteredTurn(
            turn=invocation.turn.model_copy(
                update={"invocation_id": core.InvocationId(root="foreign")}
            ),
        )
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=decision_id,
            scope=invocation.scope,
            deadline_at=1000,
            request=canonical_payload,
        )
    )
    serialized = json.dumps(
        decision.model_dump(mode="json", serialize_as_any=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    receipt = core.DecisionReceipt(
        decision_id=decision_id,
        decision=decision,
        payload_digest=sha256(serialized.encode()).hexdigest(),
        feedback=(
            core.Rejected(
                decision_id=decision_id,
                code=core.RejectionCode.BUDGET,
                path=("decision",),
                detail="denied",
            )
            if receipt_kind == "rejected"
            else core.Accepted(decision_id=decision_id)
        ),
        request_ids=(request_id,),
    )
    intent = core.Intent(
        request_id=request_id,
        request=request,
        payload_digest="registered-turn",
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.DISPATCHED,
        reconcile_deadline_at=1000,
    )
    if receipt_kind == "no-membership":
        receipt = receipt.model_copy(update={"request_ids": ()})
    elif receipt_kind == "foreign-decision":
        request = request.model_copy(update={"decision_id": core.DecisionId(root="foreign")})
        intent = intent.model_copy(update={"request": request})
    invocation = invocation.model_copy(update={"registered_operation": operation_id})
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "receipts": () if receipt_kind == "absent" else (receipt,),
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                }
            ),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
            "sessions": state.sessions.model_copy(update={"invocations": (invocation,)}),
        }
    )
    return state, codec


def restored(state: core.CoreState, codec: core.OperationRegistry) -> core.CoreState:
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=0),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=state.revision),
    )
    return codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    ).core


@pytest.mark.parametrize(
    "receipt_kind",
    ["absent", "rejected", "mismatched", "exact", "no-membership", "foreign-decision"],
)
@pytest.mark.parametrize("episode", ["absent", "old", "current"])
@pytest.mark.parametrize("correlation", ["exact", "unrelated"])
@pytest.mark.parametrize("action", ["billing", "checkpoint", "refund"])
@pytest.mark.parametrize(
    "lifecycle",
    [
        (phase, status)
        for phase in (core.SessionPhase.EXECUTING, core.SessionPhase.ACQUIRING)
        for status in (core.ObservationStatus.SUCCEEDED, core.ObservationStatus.REJECTED)
    ],
)
def test_registered_turn_requires_exact_canonical_operation_authority(
    receipt_kind: str,
    episode: str,
    correlation: str,
    action: str,
    lifecycle: tuple[core.SessionPhase, core.ObservationStatus],
) -> None:
    state, codec = registered_state(receipt_kind, episode, correlation, lifecycle)
    owner = state.attempts.attempts[0]
    invocation = state.sessions.invocations[0].invocation
    attempt = core.AttemptRef(attempt_id=owner.attempt_id, generation=owner.generation)
    paid = core.ChargeReceipt(
        charge_id=core.ChargeId(root="paid"),
        kind=core.ChargeKind.ATTEMPT,
        charged=1,
        invocation_id=invocation.invocation_id,
    )
    checkpoint = core.AttemptCheckpoint(
        invocation=invocation,
        request_id=core.RequestId(root="checkpoint"),
        revision=state.run.facts.baseline,
        retention="wip",
    )
    if action == "billing":
        event = core.InvocationChargeRequested(attempt=attempt, invocation=invocation)
    elif action == "checkpoint":
        state = state.model_copy(
            update={
                "attempts": core.AttemptsState(
                    attempts=(owner.model_copy(update={"charges": (paid,)}),)
                )
            }
        )
        event = core.InvocationCheckpointRequested(
            attempt=attempt, invocation=invocation, retention="wip", authority=checkpoint.request_id
        )
    else:
        state = state.model_copy(
            update={
                "attempts": core.AttemptsState(
                    attempts=(
                        owner.model_copy(update={"charges": (paid,), "checkpoints": (checkpoint,)}),
                    )
                ),
                "sessions": state.sessions.model_copy(
                    update={
                        "interrupts": (
                            state.sessions.interrupts[0].model_copy(
                                update={"phase": "checkpointed"}
                            ),
                        )
                    }
                ),
            }
        )
        event = core.AttemptChargeRefundRequested(
            attempt=attempt,
            charge_id=paid.charge_id,
            amount=1,
            reason="interrupted",
            authority=core.RequestId(root="interrupt"),
            checkpoint_authority=checkpoint.request_id,
        )
    state = restored(state, codec)
    result = core.advance_attempt(state.attempts, context(state), event)
    assert result == core.advance_attempt(state.attempts, context(state), event)
    prepared_origin = (
        action == "billing"
        and lifecycle[0] == core.SessionPhase.ACQUIRING
        and episode == "absent"
        and receipt_kind in ("exact", "no-membership", "foreign-decision")
    )
    proven = prepared_origin or (
        receipt_kind == "exact"
        and (
            (episode == "absent" or (correlation == "exact" and episode == "current"))
            if action == "billing"
            else correlation == "exact" and episode == "current"
        )
    )
    if action == "billing":
        assert sum(row.charged for row in result.state.attempts[0].charges) == 2 * int(proven)
    elif action == "checkpoint":
        assert (
            any(isinstance(request, core.SnapshotAndRetain) for request in result.requests)
            == proven
        )
    else:
        assert sum(row.refunded for row in result.state.attempts[0].charges) == int(proven)
    if not proven:
        assert result.state == state.attempts
