"""Mixed FIFO admission resolves the original registered reopen operation."""

from typing import Literal

import pytest

import vs_core.api as core


def normalize_reopen(request: core.OperationRequest) -> core.ScopeReopenNormalization:
    assert isinstance(request, core.ScopedAdmissionReopen)
    return core.ScopeReopenNormalization(
        attempt=request.attempt,
        continuation_id=request.continuation_id,
        park_authority=request.park_authority,
        resolved_cancelled_jobs=request.resolved_cancelled_jobs,
    )


def reopen_fixture() -> tuple[core.CoreState, core.OperationRegistry, core.Operation]:
    descriptor = core.OperationDescriptor(
        kind="evaluation.scope.reopen",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="scope-reopen", version=1),
        outcome_schema=core.SchemaRef(name="scope-reopened", version=1),
        inspect=True,
        normalization=core.OperationNormalizationKind.SCOPE_REOPEN,
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor,
                request_model=core.ScopedAdmissionReopen,
                outcome_model=core.ScopedAdmissionReopenOutcome,
                normalize_scope_reopen=normalize_reopen,
            ),
        )
    )
    state = core.initial_state()
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=codec.descriptors)}
            ),
        }
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root="reopen"),
            scope=core.Scope(owner=state.run.run_id, generation=0),
            deadline_at=100.0,
            request=core.ScopedAdmissionReopen(
                attempt=core.AttemptRef(attempt_id=core.AttemptId(root="parked"), generation=0),
                continuation_id=core.ContinuationId(root="continuation"),
                park_authority=core.RequestId(root="park"),
                resolved_cancelled_jobs=(),
            ),
        )
    )
    return state, codec, decision


@pytest.mark.parametrize("corruption", ["none", "target", "request", "pools"])
def test_reopened_admission_uses_canonical_target_request_and_capacity(
    corruption: Literal["none", "target", "request", "pools"],
) -> None:
    state, codec, decision = reopen_fixture()
    normalization = decision.normalized_scope_reopen
    assert normalization is not None
    original = core.ExecuteRegisteredOperation(
        request_id=core.RequestId(root="operation:reopen"),
        scope=decision.scope,
        deadline_at=decision.deadline_at,
        operation_id=core.OperationId(root="operation:reopen"),
        operation=codec.encode(decision.request),
        retry_limit=state.run.limits.max_retries,
    )
    assert original.request_id is not None
    continuation_guard = core.ContinuationReopenRequested(
        request=original, normalization=normalization
    )
    attempts_guard = core.ScopeReopenRequested(request=original, normalization=normalization)
    queue_request = core.AttemptReopenRequest(
        decision_id=decision.decision_id,
        request_id=original.request_id,
        attempt=normalization.attempt,
    )
    if corruption == "target":
        queue_request = queue_request.model_copy(
            update={
                "attempt": core.AttemptRef(
                    attempt_id=core.AttemptId(root="different"), generation=0
                )
            }
        )
    elif corruption == "request":
        queue_request = queue_request.model_copy(
            update={"request_id": core.RequestId(root="different")}
        )
    elif corruption == "pools":
        queue_request = queue_request.model_copy(update={"pools": (core.PoolId(root="different"),)})
    queue_signal = core.AttemptReopenRequested(request=queue_request)
    slot = core.Slot(
        attempt=normalization.attempt, admission_id=decision.decision_id, admitted_at=0.0
    )
    frames = [
        core.TraceFrame(
            signal=continuation_guard,
            change=core.EvaluationChange(state=state.evaluation, signals=(attempts_guard,)),
        ),
        core.TraceFrame(
            signal=attempts_guard,
            change=core.AttemptsChange(state=state.attempts, signals=(queue_signal,)),
        ),
        core.TraceFrame(
            signal=queue_signal,
            change=core.SchedulingChange(
                state=core.SchedulingState(slots=(slot,)),
                signals=(core.AdmitAttempt(request=queue_request),),
            ),
        ),
    ]
    if corruption == "none":
        frames.append(
            core.TraceFrame(
                signal=core.ScopeReopenAdmitted(
                    attempt=normalization.attempt,
                    request_id=original.request_id,
                    admission_id=decision.decision_id,
                ),
                change=core.AttemptsChange(state=state.attempts),
            )
        )
    event = core.DecisionSubmitted(decision=decision, expected_revision=0)
    trace = core.ReducerTrace(frames=tuple(frames))
    if corruption == "none":
        result = core.trace_step(state, event, trace)
        assert isinstance(result.events[0], core.Accepted)
        assert result.requests == ()
        assert result.state.scheduling.slots == (slot,)
    else:
        with pytest.raises(core.ContractError, match="admission"):
            core.trace_step(state, event, trace)


def test_scope_reopening_rejects_attempt_scoped_registered_operation() -> None:
    state, codec, decision = reopen_fixture()
    normalization = decision.normalized_scope_reopen
    assert normalization is not None
    owner = core.AttemptView(
        attempt_id=normalization.attempt.attempt_id,
        item_id=core.ItemId(root="item"),
        generation=normalization.attempt.generation,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    scoped = codec.validate_decision(
        decision.model_copy(
            update={
                "scope": core.Scope(owner=owner.attempt_id, generation=owner.generation),
            }
        )
    )
    result = core.step(state, core.DecisionSubmitted(decision=scoped, expected_revision=0))
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].code == core.RejectionCode.OWNERSHIP
    assert result.events[0].path == ("scope", "owner")
    assert result.requests == ()
