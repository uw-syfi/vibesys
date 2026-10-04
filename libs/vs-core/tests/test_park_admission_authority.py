"""Park cleanup authority belongs to the owner's current admission episode."""

from itertools import product
from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

Episode = Literal["missing", "matching", "different"]


def normalize_reopen(request: core.OperationRequest) -> core.ScopeReopenNormalization:
    assert isinstance(request, core.ScopedAdmissionReopen)
    return core.ScopeReopenNormalization(
        attempt=request.attempt,
        continuation_id=request.continuation_id,
        park_authority=request.park_authority,
        resolved_cancelled_jobs=request.resolved_cancelled_jobs,
    )


def parked_state(episode: str) -> tuple[core.CoreState, core.ContinuationReopenRequested]:
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=core.OperationDescriptor(
                    kind="evaluation.scope.reopen",
                    lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
                    request_schema=core.SchemaRef(name="reopen", version=1),
                    outcome_schema=core.SchemaRef(name="reopened", version=1),
                    inspect=True,
                    normalization=core.OperationNormalizationKind.SCOPE_REOPEN,
                ),
                request_model=core.ScopedAdmissionReopen,
                outcome_model=core.ScopedAdmissionReopenOutcome,
                normalize_scope_reopen=normalize_reopen,
            ),
        )
    )
    state = core.initial_state()
    admission = core.DecisionId(root=episode)
    attempt = core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0)
    scope = core.Scope(owner=attempt.attempt_id, generation=attempt.generation)
    park = core.RequestId(root="park")
    invocation = core.InvocationRef(
        session_id=core.SessionId(root="session"),
        invocation_id=core.InvocationId(root="yielded"),
        generation=0,
    )
    session = core.SessionSpec(
        session_id=invocation.session_id,
        role_id=core.RoleId(root="role"),
        policy="reuse",
        lifetime="owner",
        access=core.Access.READ_ONLY,
    )
    record = core.Invocation(
        invocation=invocation,
        scope=scope,
        phase=core.SessionPhase.SUSPENDED,
        turn=core.TurnSpec(
            session=session,
            invocation_id=invocation.invocation_id,
            workspace=scope,
            prompts=(),
            output_schema=core.SchemaRef(name="output", version=1),
            deadline_at=100.0,
            charge_class="paid",
        ),
    )
    job = core.RegisteredOwnedJob(
        operation_id=core.OperationId(root="job-operation"),
        request_id=core.RequestId(root="submission"),
        scope=scope,
        resource_pool=core.PoolId(root="pool"),
        resource_id=core.ResourceId(root="job"),
        status=core.ObservationStatus.SUCCEEDED,
        terminal=True,
        released=True,
        observation=core.Observation(
            event_id=core.EventId(root="job-observation"),
            request_id=core.RequestId(root="submission"),
            scope=scope,
            resource_id=core.ResourceId(root="job"),
            sequence=1,
            observed_at=1.0,
            status=core.ObservationStatus.SUCCEEDED,
            accepted=True,
            terminal=True,
            released=True,
            children_complete=True,
        ),
    )
    assert job.resource_id is not None
    continuation = core.Continuation(
        continuation_id=core.ContinuationId(root="wait"),
        invocation=invocation,
        next_invocation=invocation.model_copy(
            update={"invocation_id": core.InvocationId(root="resume")}
        ),
        jobs=(job.resource_id,),
        deadline_at=10.0,
        phase=core.ContinuationPhase.PARKED,
        park_authority=park,
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="reopen"),
            scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
            deadline_at=100.0,
            request=core.ScopedAdmissionReopen(
                attempt=attempt,
                continuation_id=continuation.continuation_id,
                park_authority=park,
                resolved_cancelled_jobs=(),
            ),
        )
    )
    request = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="operation:reopen"),
        operation_id=core.OperationId(root="operation:reopen"),
        operation=codec.encode(decision.request),
        scope=decision.scope,
        deadline_at=decision.deadline_at,
        retry_limit=state.run.limits.max_retries,
    )
    assert request.request_id is not None
    close = core.CloseAttemptScope(
        request_id=park,
        scope=scope,
        deadline_at=100.0,
        attempt=attempt,
        admission_id=admission,
    )
    assert job.observation is not None
    close_intent = core.Intent(
        request_id=park,
        request=close,
        payload_digest="close",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.COMPLETED,
        reconcile_deadline_at=100.0,
        observation=job.observation.model_copy(
            update={"request_id": park, "resource_id": None, "admission_id": admission}
        ),
    )
    owner = core.AttemptView(
        attempt_id=attempt.attempt_id,
        item_id=core.ItemId(root="item"),
        generation=attempt.generation,
        phase=core.AttemptPhase.PARKED,
        admission_id=admission,
        closure=core.AttemptClosure(
            disposition="park", requested_at=1.0, authority=park, admission_id=admission
        ),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                    "receipts": (
                        core.DecisionReceipt(
                            decision_id=decision.decision_id,
                            decision=decision,
                            payload_digest="reopen",
                            feedback=core.Accepted(decision_id=decision.decision_id),
                            request_ids=(request.request_id,),
                        ),
                    ),
                }
            ),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "sessions": core.SessionsState(invocations=(record,)),
            "evaluation": core.EvaluationState(
                registered_jobs=(job,), continuations=(continuation,)
            ),
            "intents": state.intents.model_copy(update={"intents": (close_intent,)}),
        }
    )
    assert decision.normalized_scope_reopen is not None
    return state, core.ContinuationReopenRequested(
        request=request, normalization=decision.normalized_scope_reopen
    )


def admission_identity(kind: Episode, episode: str) -> core.DecisionId | None:
    if kind == "missing":
        return None
    return core.DecisionId(root=episode if kind == "matching" else f"foreign:{episode}")


@pytest.mark.parametrize(
    ("owner_episode", "request_episode"), product(("missing", "matching", "different"), repeat=2)
)
@pytest.mark.parametrize("action", ["park", "reopen"])
@given(episode=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=32))
def test_cleanup_requires_owner_closure_and_request_in_same_present_episode(
    owner_episode: Episode, request_episode: Episode, action: str, episode: str
) -> None:
    state, reopen_event = parked_state(episode)
    event: core.CoreEvent = reopen_event
    owner = state.attempts.attempts[0].model_copy(
        update={"admission_id": admission_identity(owner_episode, episode)}
    )
    intent = state.intents.intents[0]
    assert intent.observation is not None
    request_admission = admission_identity(request_episode, episode)
    intent = intent.model_copy(
        update={
            "request": intent.request.model_copy(update={"admission_id": request_admission}),
            "observation": intent.observation.model_copy(
                update={"admission_id": request_admission}
            ),
        }
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    if action == "park":
        continuation = state.evaluation.continuations[0].model_copy(
            update={"phase": core.ContinuationPhase.WAITING, "park_authority": None}
        )
        state = state.model_copy(
            update={
                "evaluation": state.evaluation.model_copy(update={"continuations": (continuation,)})
            }
        )
        event = core.ContinuationRetireRequested(
            continuation_id=continuation.continuation_id,
            disposition="park",
            park_authority=core.RequestId(root="park"),
        )
    before = state.model_dump_json()
    if owner_episode != "matching" or request_episode != "matching":
        with pytest.raises(core.ContractError, match=r"cleanup proof|scope close"):
            core.step(state, event)
    elif action == "park":
        result = core.step(state, event)
        assert result.state.evaluation.continuations[0].phase == core.ContinuationPhase.PARKED
    else:
        # Public step reaches the Attempts B owner only after forwarding the guarded signal.
        with pytest.raises(core.KernelNotImplementedError) as forwarded:
            core.step(state, event)
        assert forwarded.value.area == core.Area.ATTEMPTS
        assert forwarded.value.event_kind == core.ScopeReopenRequested.model_fields["kind"].default
    assert state.model_dump_json() == before
