"""Dispatch fences cannot turn missing publication/history/checkpoint into proof."""

import hashlib
import json
from typing import ClassVar, Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

import vs_core.api as core


class ResumeOutput(core.Value):
    status: Literal["succeeded"] = "succeeded"


class RegisteredResume(core.OperationRequest):
    kind: Literal["test.resume"] = "test.resume"
    lifecycle: Literal[core.LifecycleClass.SESSION_TURN] = core.LifecycleClass.SESSION_TURN
    outcome_model: ClassVar[type[BaseModel]] = ResumeOutput
    turn: core.TurnSpec


def normalize_resume(request: core.OperationRequest) -> core.TurnSpec:
    assert isinstance(request, RegisteredResume)
    return request.turn


def resume_codec() -> core.OperationRegistry:
    return core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=core.OperationDescriptor(
                    kind="test.resume",
                    request_schema=core.SchemaRef(name="resume", version=1),
                    outcome_schema=core.SchemaRef(name="output", version=1),
                    lifecycle=core.LifecycleClass.SESSION_TURN,
                    inspect=True,
                    cancel=True,
                    watch=True,
                ),
                request_model=RegisteredResume,
                outcome_model=ResumeOutput,
                normalize_turn=normalize_resume,
            ),
        )
    )


def digest(value: core.Value) -> str:
    source = json.dumps(value.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(source.encode()).hexdigest()


def resume_state(
    *, attempt_owned: bool, writable: bool, registered: bool
) -> tuple[core.CoreState, core.RequestId]:
    state = core.initial_state()
    scope = core.Scope(
        owner=core.AttemptId(root="owner") if attempt_owned else state.run.run_id, generation=0
    )
    before = core.InvocationRef(
        session_id=core.SessionId(root="session"),
        invocation_id=core.InvocationId(root="before"),
        generation=0,
    )
    successor = before.model_copy(update={"invocation_id": core.InvocationId(root="next")})
    continuation_id = core.ContinuationId(root="wait")
    turn = core.TurnSpec(
        session=core.SessionSpec(
            session_id=before.session_id,
            role_id=core.RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=core.Access.WRITE_CANDIDATE if writable else core.Access.READ_ONLY,
        ),
        invocation_id=successor.invocation_id,
        continuation_id=continuation_id,
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="output", version=1),
        deadline_at=100.0,
        charge_class="resume",
    )
    prior_turn = turn.model_copy(
        update={
            "invocation_id": before.invocation_id,
            "continuation_id": None,
            "charge_class": "free",
        }
    )
    prior = core.Invocation(
        invocation=before,
        scope=scope,
        turn=prior_turn,
        evaluation_prefix=core.EvaluationHistoryCursor() if attempt_owned else None,
        phase=core.SessionPhase.CHECKPOINTED if writable else core.SessionPhase.TERMINAL,
    )
    publication = core.ResumeAuthorizationReceipt(
        continuation_id=continuation_id,
        next_invocation=successor,
        evidence=(),
        history_cursor=core.EvaluationHistoryCursor(),
    )
    continuation = core.Continuation(
        continuation_id=continuation_id,
        invocation=before,
        next_invocation=successor,
        jobs=(),
        deadline_at=80.0,
        phase=core.ContinuationPhase.AUTHORIZED,
        authorization_receipt=publication,
    )
    admission = core.DecisionId(root="admission") if attempt_owned else None
    decision_id = core.DecisionId(root="resume")
    identity = core.RequestId(root="resume-dispatch")
    request: core.ResumeSessionTurn | core.ExecuteRegisteredOperation
    if registered:
        codec = resume_codec()
        decision = codec.validate_decision(
            core.Operation(
                decision_id=decision_id,
                scope=scope,
                request=RegisteredResume(turn=turn),
                deadline_at=100.0,
            )
        )
        request = core.ExecuteRegisteredOperation(
            request_id=identity,
            scope=scope,
            admission_id=admission,
            decision_id=decision_id,
            deadline_at=100.0,
            operation_id=core.OperationId(root="operation:resume"),
            operation=codec.encode(decision.request),
            retry_limit=0,
        )
        state = state.model_copy(
            update={
                "registry": codec.descriptors,
                "run": state.run.model_copy(
                    update={"capabilities": core.Capabilities(operations=codec.descriptors)}
                ),
            }
        )
    else:
        decision = core.RequestTurn(decision_id=decision_id, scope=scope, turn=turn)
        request = core.ResumeSessionTurn(
            request_id=identity,
            scope=scope,
            admission_id=admission,
            decision_id=decision_id,
            deadline_at=100.0,
            turn=turn,
            continuation_id=continuation_id,
        )
    receipt = core.DecisionReceipt(
        decision_id=decision_id,
        decision=decision,
        payload_digest=digest(decision),
        feedback=core.Accepted(decision_id=decision_id, request_ids=(identity,)),
        request_ids=(identity,),
    )
    intent = core.Intent(
        request_id=identity,
        request=request,
        payload_digest=digest(request),
        lifecycle=core.LifecycleClass.SESSION_TURN,
        phase=core.IntentPhase.PREPARED,
        reconcile_deadline_at=100.0,
    )
    owners = ()
    if attempt_owned:
        owners = (
            core.AttemptView(
                attempt_id=core.AttemptId(root="owner"),
                item_id=core.ItemId(root="item"),
                generation=0,
                phase=core.AttemptPhase.ACTIVE,
                workspace=core.WorkspacePlan(
                    mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
                ),
                budget=core.AttemptBudget(),
                admission_id=admission,
                evaluation_history=core.AttemptEvaluationHistory(
                    availability=core.EvaluationHistoryAvailability.COMPLETE
                ),
            ),
        )
    checkpoints = ()
    if writable and not attempt_owned:
        checkpoints = (
            core.RunInvocationCheckpoint(
                invocation=before,
                scope=scope,
                request_id=core.RequestId(root="checkpoint"),
                revision=state.run.facts.baseline,
                retention="wip",
            ),
        )
    return state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (receipt,)}),
            "attempts": core.AttemptsState(attempts=owners),
            "evaluation": core.EvaluationState(continuations=(continuation,)),
            "sessions": core.SessionsState(invocations=(prior,), run_checkpoints=checkpoints),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    ), identity


def roundtrip_state(state: core.CoreState, *, registered: bool) -> core.CoreState:
    codec = resume_codec() if registered else core.OperationRegistry()
    envelope = core.RunEnvelope[core.StrategyState](
        schema_version=core.ENVELOPE_SCHEMA_VERSION,
        fence=core.HostFence(host_id=core.HostId(root="host"), epoch=0),
        strategy_id=state.run.declaration.strategy_id,
        state_schema=state.run.declaration.state_schema,
        core=state,
        strategy=core.StrategyState(schema_version=1),
        event_cursor=core.EventCursor(sequence=0),
    )
    restored = codec.decode_envelope(
        core.RunEnvelope[core.StrategyState], codec.encode_envelope(envelope)
    )
    assert restored.core == state
    return restored.core


@given(attempt_owned=st.booleans(), writable=st.booleans(), registered=st.booleans())
def test_exact_resume_proofs_reach_dispatch_without_new_charges(
    *, attempt_owned: bool, writable: bool, registered: bool
) -> None:
    state, identity = resume_state(
        attempt_owned=attempt_owned, writable=writable, registered=registered
    )
    state = roundtrip_state(state, registered=registered)
    event = core.DispatchAuthorized(request_id=identity)
    before = state.model_dump_json()
    result = core.trace_step(
        state,
        event,
        core.ReducerTrace(
            frames=(core.TraceFrame(signal=event, change=core.IntentsChange(state=state.intents)),)
        ),
    )
    assert result.requests == ()
    assert result.state.attempts == state.attempts
    assert result.state.sessions == state.sessions
    assert state.model_dump_json() == before
    # Reordered replay preserves publication, ownership and currency.
    replay = core.trace_step(
        result.state,
        event,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=event, change=core.IntentsChange(state=result.state.intents)
                ),
            )
        ),
    )
    assert replay.state.evaluation == state.evaluation
    assert replay.state.attempts == state.attempts


@pytest.mark.parametrize(
    "fault", ["missing", "successor", "continuation", "phase", "prior-scope", "prior-missing"]
)
@given(attempt_owned=st.booleans(), registered=st.booleans())
def test_resume_dispatch_rejects_missing_or_foreign_publication_proof(
    fault: str, *, attempt_owned: bool, registered: bool
) -> None:
    state, identity = resume_state(
        attempt_owned=attempt_owned, writable=False, registered=registered
    )
    continuation = state.evaluation.continuations[0]
    assert continuation.authorization_receipt is not None
    match fault:
        case "missing":
            continuation = continuation.model_copy(update={"authorization_receipt": None})
        case "successor":
            proof = continuation.authorization_receipt.model_copy(
                update={"next_invocation": continuation.invocation}
            )
            continuation = continuation.model_copy(update={"authorization_receipt": proof})
        case "continuation":
            proof = continuation.authorization_receipt.model_copy(
                update={"continuation_id": core.ContinuationId(root="other")}
            )
            continuation = continuation.model_copy(update={"authorization_receipt": proof})
        case "phase":
            continuation = continuation.model_copy(update={"phase": core.ContinuationPhase.PARKED})
        case "prior-scope":
            prior = state.sessions.invocations[0].model_copy(
                update={"scope": core.Scope(owner=core.AttemptId(root="other"), generation=0)}
            )
            state = state.model_copy(
                update={"sessions": state.sessions.model_copy(update={"invocations": (prior,)})}
            )
        case "prior-missing":
            state = state.model_copy(
                update={"sessions": state.sessions.model_copy(update={"invocations": ()})}
            )
    state = state.model_copy(
        update={
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)})
        }
    )
    with pytest.raises(core.ContractError, match="published successor"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


@pytest.mark.parametrize("fault", ["unavailable", "exhausted"])
@given(registered=st.booleans())
def test_attempt_resume_dispatch_requires_complete_unexhausted_history(
    fault: str, *, registered: bool
) -> None:
    state, identity = resume_state(attempt_owned=True, writable=False, registered=registered)
    owner = state.attempts.attempts[0]
    if fault == "unavailable":
        owner = owner.model_copy(update={"evaluation_history": core.AttemptEvaluationHistory()})
    else:
        owner = owner.model_copy(
            update={"terminal_reason": core.AttemptTerminalReason.REPEATED_TRACEBACK}
        )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    with pytest.raises(core.ContractError, match="complete unexhausted"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


@pytest.mark.parametrize("fault", ["missing", "invocation", "scope"])
@given(registered=st.booleans())
def test_run_writer_resume_dispatch_requires_exact_retained_predecessor(
    fault: str, *, registered: bool
) -> None:
    state, identity = resume_state(attempt_owned=False, writable=True, registered=registered)
    proof = state.sessions.run_checkpoints[0]
    match fault:
        case "missing":
            checkpoints = ()
        case "invocation":
            checkpoints = (
                proof.model_copy(
                    update={
                        "invocation": proof.invocation.model_copy(
                            update={"invocation_id": core.InvocationId(root="other")}
                        )
                    }
                ),
            )
        case "scope":
            checkpoints = (
                proof.model_copy(
                    update={"scope": core.Scope(owner=core.RunId(root="other"), generation=0)}
                ),
            )
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"run_checkpoints": checkpoints})}
    )
    with pytest.raises(core.ContractError, match="predecessor checkpoint"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


def test_registered_session_dispatch_cannot_classify_absent_receipt_as_ordinary() -> None:
    state, identity = resume_state(attempt_owned=False, writable=False, registered=True)
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": ()})})
    with pytest.raises(core.ContractError, match=r"canonical turn proof|successful dependency"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


@pytest.mark.parametrize("fault", ["missing", "beyond-history", "foreign-submission"])
@given(registered=st.booleans())
def test_attempt_resume_requires_exact_paid_cycle_history_prefix(
    fault: str, *, registered: bool
) -> None:
    state, identity = resume_state(attempt_owned=True, writable=False, registered=registered)
    prior = state.sessions.invocations[0]
    if fault == "missing":
        prefix = None
    else:
        prefix = core.EvaluationHistoryCursor(
            ordinal=1, submission_id=core.RequestId(root="foreign")
        )
    if fault == "foreign-submission":
        owner = state.attempts.attempts[0]
        source = core.RequestId(root="submission")
        scope = prior.scope
        observed = core.Observation(
            event_id=core.EventId(root="terminal"),
            request_id=source,
            scope=scope,
            sequence=1,
            observed_at=1.0,
            status=core.ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
        )
        record = core.AttemptEvaluationRecord(
            ordinal=1, submission_id=source, scope=scope, terminal_observation=observed
        )
        history = core.AttemptEvaluationHistory(
            availability=core.EvaluationHistoryAvailability.COMPLETE,
            covered_submissions=(source,),
            records=(record,),
        )
        state = state.model_copy(
            update={
                "attempts": core.AttemptsState(
                    attempts=(owner.model_copy(update={"evaluation_history": history}),)
                )
            }
        )
    prior = prior.model_copy(update={"evaluation_prefix": prefix})
    state = state.model_copy(
        update={"sessions": state.sessions.model_copy(update={"invocations": (prior,)})}
    )
    with pytest.raises(core.ContractError, match="paid-cycle history prefix"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


@pytest.mark.parametrize("fault", ["registry", "capability", "schema", "descriptor"])
@given(attempt_owned=st.booleans())
def test_registered_session_requires_exact_offered_descriptor(
    fault: str, *, attempt_owned: bool
) -> None:
    state, identity = resume_state(attempt_owned=attempt_owned, writable=False, registered=True)
    descriptor = state.registry[0]
    match fault:
        case "registry":
            state = state.model_copy(update={"registry": ()})
        case "capability":
            state = state.model_copy(
                update={"run": state.run.model_copy(update={"capabilities": core.Capabilities()})}
            )
        case "schema":
            descriptor = descriptor.model_copy(
                update={"request_schema": core.SchemaRef(name="foreign", version=1)}
            )
            state = state.model_copy(
                update={
                    "registry": (descriptor,),
                    "run": state.run.model_copy(
                        update={"capabilities": core.Capabilities(operations=(descriptor,))}
                    ),
                }
            )
        case "descriptor":
            changed = descriptor.model_copy(update={"inspect": False})
            state = state.model_copy(
                update={
                    "run": state.run.model_copy(
                        update={"capabilities": core.Capabilities(operations=(changed,))}
                    )
                }
            )
    with pytest.raises(core.ContractError, match=r"canonical turn proof|successful dependency"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


@pytest.mark.parametrize("fault", ["beyond-history", "foreign-submission", "before-cycle"])
@given(registered=st.booleans())
def test_publication_history_cursor_requires_exact_current_attempt_prefix(
    fault: str, *, registered: bool
) -> None:
    state, identity = resume_state(attempt_owned=True, writable=False, registered=registered)
    prior = state.sessions.invocations[0]
    source = core.RequestId(root="submission")
    observed = core.Observation(
        event_id=core.EventId(root="terminal"),
        request_id=source,
        scope=prior.scope,
        sequence=1,
        observed_at=1.0,
        status=core.ObservationStatus.SUCCEEDED,
        accepted=True,
        terminal=True,
    )
    history = core.AttemptEvaluationHistory(
        availability=core.EvaluationHistoryAvailability.COMPLETE,
        covered_submissions=(source,),
        records=(
            core.AttemptEvaluationRecord(
                ordinal=1, submission_id=source, scope=prior.scope, terminal_observation=observed
            ),
        ),
    )
    owner = state.attempts.attempts[0].model_copy(update={"evaluation_history": history})
    continuation = state.evaluation.continuations[0]
    assert continuation.authorization_receipt is not None
    if fault == "before-cycle":
        cursor = core.EvaluationHistoryCursor()
        prior = prior.model_copy(update={"evaluation_prefix": history.cursor})
    else:
        cursor = core.EvaluationHistoryCursor(
            ordinal=2 if fault == "beyond-history" else 1,
            submission_id=core.RequestId(root="foreign"),
        )
    publication = continuation.authorization_receipt.model_copy(update={"history_cursor": cursor})
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": state.sessions.model_copy(update={"invocations": (prior,)}),
            "evaluation": state.evaluation.model_copy(
                update={
                    "continuations": (
                        continuation.model_copy(update={"authorization_receipt": publication}),
                    )
                }
            ),
        }
    )
    with pytest.raises(core.ContractError, match="publication history prefix"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


@pytest.mark.parametrize("fault", ["writable-predecessor", "session", "same-invocation"])
@given(attempt_owned=st.booleans(), registered=st.booleans())
def test_resume_dispatch_preserves_session_authority_and_distinct_successor(
    fault: str, *, attempt_owned: bool, registered: bool
) -> None:
    state, identity = resume_state(
        attempt_owned=attempt_owned, writable=False, registered=registered
    )
    prior = state.sessions.invocations[0]
    continuation = state.evaluation.continuations[0]
    if fault == "writable-predecessor":
        session = prior.turn.session.model_copy(update={"access": core.Access.WRITE_CANDIDATE})
        prior = prior.model_copy(
            update={"turn": prior.turn.model_copy(update={"session": session})}
        )
    else:
        if fault == "session":
            reference = prior.invocation.model_copy(
                update={"session_id": core.SessionId(root="other")}
            )
            session = prior.turn.session.model_copy(update={"session_id": reference.session_id})
            prior = prior.model_copy(
                update={
                    "invocation": reference,
                    "turn": prior.turn.model_copy(update={"session": session}),
                }
            )
        else:
            reference = continuation.next_invocation
            prior = prior.model_copy(
                update={
                    "invocation": reference,
                    "turn": prior.turn.model_copy(
                        update={"invocation_id": reference.invocation_id}
                    ),
                }
            )
        continuation = continuation.model_copy(update={"invocation": reference})
    state = state.model_copy(
        update={
            "sessions": state.sessions.model_copy(update={"invocations": (prior,)}),
            "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)}),
        }
    )
    state = roundtrip_state(state, registered=registered)
    with pytest.raises(core.ContractError, match="published successor proof"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


def test_registered_session_dispatch_rejects_inner_decision_identity_conflict() -> None:
    state, identity = resume_state(attempt_owned=False, writable=False, registered=True)
    receipt = state.run.receipts[0]
    assert isinstance(receipt.decision, core.Operation)
    changed = receipt.decision.model_copy(update={"decision_id": core.DecisionId(root="foreign")})
    receipt = receipt.model_copy(update={"decision": changed, "payload_digest": digest(changed)})
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": (receipt,)})})
    state = roundtrip_state(state, registered=True)
    with pytest.raises(core.ContractError, match=r"canonical turn proof|successful dependency"):
        core.step(state, core.DispatchAuthorized(request_id=identity))
