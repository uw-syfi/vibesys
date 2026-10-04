"""Builtin turn dispatch requires the accepted canonical RequestTurn origin."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .test_resume_dispatch_proof import digest, resume_state, roundtrip_state


def builtin_state(
    *, attempt_owned: bool, writable: bool, dispatch: bool
) -> tuple[core.CoreState, core.RequestId]:
    state, identity = resume_state(attempt_owned=attempt_owned, writable=writable, registered=False)
    if dispatch:
        intent = state.intents.intents[0]
        assert isinstance(intent.request, core.ResumeSessionTurn)
        request = core.DispatchTurn.model_validate(
            intent.request.model_dump(exclude={"kind", "continuation_id"})
        )
        state = replace_request(state, request)
    return state, identity


def replace_request(
    state: core.CoreState, request: core.DispatchTurn | core.ResumeSessionTurn
) -> core.CoreState:
    intent = state.intents.intents[0].model_copy(
        update={"request": request, "payload_digest": digest(request)}
    )
    return state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (intent,)})}
    )


def corrupt_origin(state: core.CoreState, fault: str) -> core.CoreState:
    if fault == "missing-receipt":
        return state.model_copy(update={"run": state.run.model_copy(update={"receipts": ()})})
    receipt = state.run.receipts[0]
    assert isinstance(receipt.decision, core.RequestTurn)
    decision = receipt.decision
    request = state.intents.intents[0].request
    assert isinstance(request, core.ResumeSessionTurn | core.DispatchTurn)
    match fault:
        case "feedback-id":
            receipt = receipt.model_copy(
                update={"feedback": core.Accepted(decision_id=core.DecisionId(root="foreign"))}
            )
        case "decision-id":
            decision = decision.model_copy(update={"decision_id": core.DecisionId(root="foreign")})
        case "membership":
            receipt = receipt.model_copy(update={"request_ids": ()})
        case "decision-type":
            decision = core.Stop(
                decision_id=decision.decision_id,
                scope=decision.scope,
                mode="drain",
                result=core.RunResultProposal(outcome="cancelled", reason="stop"),
            )
        case "scope":
            decision = decision.model_copy(
                update={"scope": core.Scope(owner=core.RunId(root="foreign"), generation=0)}
            )
        case "request-turn":
            turn = request.turn.model_copy(
                update={"output_schema": core.SchemaRef(name="foreign-output", version=1)}
            )
            state = replace_request(state, request.model_copy(update={"turn": turn}))
        case "deadline":
            state = replace_request(state, request.model_copy(update={"deadline_at": 99.0}))
        case "request-origin":
            state = replace_request(
                state, request.model_copy(update={"decision_id": core.DecisionId(root="foreign")})
            )
    receipt = receipt.model_copy(update={"decision": decision, "payload_digest": digest(decision)})
    return state.model_copy(update={"run": state.run.model_copy(update={"receipts": (receipt,)})})


def assert_origin_rejected(state: core.CoreState, identity: core.RequestId) -> None:
    restored = roundtrip_state(state, registered=False)
    event = core.DispatchAuthorized(request_id=identity)
    with pytest.raises(core.ContractError, match="canonical turn proof"):
        core.trace_step(
            restored,
            event,
            core.ReducerTrace(
                frames=(
                    core.TraceFrame(
                        signal=event, change=core.IntentsChange(state=restored.intents)
                    ),
                )
            ),
        )
    with pytest.raises(core.ContractError, match="canonical turn proof"):
        core.step(restored, event)


@pytest.mark.parametrize(
    "fault",
    [
        "missing-receipt",
        "feedback-id",
        "decision-id",
        "membership",
        "decision-type",
        "scope",
        "request-turn",
        "deadline",
        "request-origin",
    ],
)
@given(attempt_owned=st.booleans(), writable=st.booleans(), dispatch=st.booleans())
def test_builtin_resume_rejects_missing_or_foreign_canonical_origin(
    fault: str, *, attempt_owned: bool, writable: bool, dispatch: bool
) -> None:
    state, identity = builtin_state(
        attempt_owned=attempt_owned, writable=writable, dispatch=dispatch
    )
    assert_origin_rejected(corrupt_origin(state, fault), identity)


@pytest.mark.parametrize("charge_class", ["free", "correction"])
@given(attempt_owned=st.booleans())
def test_builtin_dispatch_cannot_reclassify_canonical_resume_to_skip_its_proofs(
    charge_class: str, *, attempt_owned: bool
) -> None:
    state, identity = builtin_state(attempt_owned=attempt_owned, writable=False, dispatch=True)
    request = state.intents.intents[0].request
    assert isinstance(request, core.DispatchTurn)
    turn = request.turn.model_copy(update={"charge_class": charge_class})
    state = replace_request(state, request.model_copy(update={"turn": turn}))
    assert_origin_rejected(state, identity)


@given(attempt_owned=st.booleans())
def test_builtin_dispatch_cannot_erase_resume_origin_and_publication_together(
    *, attempt_owned: bool
) -> None:
    state, identity = builtin_state(attempt_owned=attempt_owned, writable=False, dispatch=True)
    request = state.intents.intents[0].request
    assert isinstance(request, core.DispatchTurn)
    turn = request.turn.model_copy(update={"charge_class": "free", "continuation_id": None})
    request = request.model_copy(update={"turn": turn, "decision_id": None})
    state = replace_request(state, request).model_copy(
        update={"evaluation": core.EvaluationState()}
    )
    assert_origin_rejected(state, identity)


@given(attempt_owned=st.booleans(), writable=st.booleans(), dispatch=st.booleans())
def test_exact_builtin_origin_and_publication_survive_reload_and_replay(
    *, attempt_owned: bool, writable: bool, dispatch: bool
) -> None:
    state, identity = builtin_state(
        attempt_owned=attempt_owned, writable=writable, dispatch=dispatch
    )
    state = roundtrip_state(state, registered=False)
    event = core.DispatchAuthorized(request_id=identity)
    before = state.model_dump_json()
    for _ in range(2):
        result = core.trace_step(
            state,
            event,
            core.ReducerTrace(
                frames=(
                    core.TraceFrame(signal=event, change=core.IntentsChange(state=state.intents)),
                )
            ),
        )
        assert result.requests == result.events == ()
        assert result.state.sessions == state.sessions
        assert result.state.attempts == state.attempts
        assert result.state.run.receipts == state.run.receipts
        assert state.model_dump_json() == before
        state = result.state
        before = state.model_dump_json()
