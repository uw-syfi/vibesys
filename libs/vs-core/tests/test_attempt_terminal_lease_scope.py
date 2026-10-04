"""Writer terminal authority includes the retained session lease scope."""

import json
from hashlib import sha256

import pytest

from vs_core.api import (
    AttemptId,
    ChargeId,
    ChargeKind,
    ChargeReceipt,
    CoreState,
    ObservationStatus,
    RequestTurn,
    ResourceId,
    RunId,
    Scope,
    SessionPhase,
    SnapshotAndRetain,
    advance_attempt,
)

from .test_attempt_invocation_proofs import checkpoint_event, context, invocation_fixture


@pytest.mark.parametrize("resource", ["absent", "exact", "mismatched"])
@pytest.mark.parametrize("status", [ObservationStatus.SUCCEEDED, ObservationStatus.REJECTED])
@pytest.mark.parametrize(
    "scope_kind", ["owner", "foreign-owner", "old-owner", "run", "foreign-run", "old-run"]
)
def test_terminal_writer_requires_exact_owner_or_current_reusable_run_lease(
    scope_kind: str,
    resource: str,
    status: ObservationStatus,
) -> None:
    state = invocation_fixture(lifecycle=(SessionPhase.EXECUTING, status))
    original = state.sessions.invocations[0]
    spec = original.turn.session.model_copy(update={"policy": "reuse", "lifetime": "owner"})
    turn = original.turn.model_copy(update={"session": spec})
    assert original.observation is not None
    observed = original.observation.model_copy(
        update={
            "resource_id": ResourceId(root="foreign-resource")
            if resource == "mismatched"
            else state.sessions.sessions[0].resource_id
            if resource == "exact"
            else None,
        }
    )
    invocation = original.model_copy(update={"turn": turn, "observation": observed})
    scopes = {
        "owner": original.scope,
        "foreign-owner": Scope(owner=AttemptId(root="foreign"), generation=0),
        "old-owner": original.scope.model_copy(update={"generation": 1}),
        "run": Scope(owner=state.run.run_id, generation=state.run.generation),
        "foreign-run": Scope(owner=RunId(root="foreign"), generation=state.run.generation),
        "old-run": Scope(owner=state.run.run_id, generation=state.run.generation + 1),
    }
    session = state.sessions.sessions[0].model_copy(
        update={
            "spec": spec,
            "scope": scopes[scope_kind],
            "resource_id": None if resource == "absent" else state.sessions.sessions[0].resource_id,
        }
    )
    canonical = state.intents.intents[0]
    intent = canonical.model_copy(
        update={
            "request": canonical.request.model_copy(update={"turn": turn}),
            "observation": observed,
        }
    )
    receipt = state.run.receipts[0]
    assert isinstance(receipt.decision, RequestTurn)
    decision = receipt.decision.model_copy(update={"turn": turn})
    digest = sha256(
        json.dumps(
            decision.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    receipt = receipt.model_copy(update={"decision": decision, "payload_digest": digest})
    owner = state.attempts.attempts[0].model_copy(
        update={
            "charges": (
                ChargeReceipt(
                    charge_id=ChargeId(root="paid"),
                    kind=ChargeKind.ATTEMPT,
                    invocation_id=invocation.invocation.invocation_id,
                    charged=1,
                ),
            )
        }
    )
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(
                update={"sessions": (session,), "invocations": (invocation,)}
            ),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
            "attempts": state.attempts.model_copy(update={"attempts": (owner,)}),
            "run": state.run.model_copy(update={"receipts": (receipt,)}),
        }
    )
    state = CoreState.model_validate(state.model_dump())
    result = advance_attempt(state.attempts, context(state), checkpoint_event(state))
    authorized = scope_kind in ("owner", "run") and (
        resource == "exact" or (resource == "absent" and status == ObservationStatus.REJECTED)
    )
    if authorized:
        assert len(result.requests) == 1
        assert isinstance(result.requests[0], SnapshotAndRetain)
    else:
        assert result.requests == ()
        assert result.state == state.attempts
