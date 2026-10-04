"""Decision completion remains a durable dependency beyond initial admission."""

from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel

import vs_core.api as core
from vs_core.api import (
    Accepted,
    AttemptBudget,
    AttemptId,
    AttemptRequest,
    AttemptRequested,
    ClockAdvanced,
    DecisionId,
    DecisionSubmitted,
    ItemId,
    Operation,
    ReducerTrace,
    SchedulingChange,
    Scope,
    StartAttempt,
    TraceFrame,
    WorkspaceMode,
    WorkspacePlan,
    trace_step,
)


class WriteOutcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class ArtifactPut(core.OperationRequest):
    kind: Literal["test.write"] = "test.write"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = WriteOutcome
    content: str


def operation_state() -> tuple[core.OperationRegistry, core.CoreState]:
    descriptor = core.OperationDescriptor(
        kind="test.write",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="write", version=1),
        outcome_schema=core.SchemaRef(name="outcome", version=1),
        inspect=True,
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=ArtifactPut, outcome_model=WriteOutcome
            ),
        )
    )
    state = core.initial_state()
    return codec, state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=codec.descriptors)}
            ),
        }
    )


def queued_start(state: core.CoreState) -> tuple[core.Transition, StartAttempt]:
    start = StartAttempt(
        decision_id=DecisionId(root="queued"),
        scope=Scope(owner=state.run.run_id, generation=0),
        attempt_id=AttemptId(root="attempt"),
        item_id=ItemId(root="item"),
        workspace=WorkspacePlan(mode=WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline),
        budget=AttemptBudget(),
    )
    signal = AttemptRequested(
        request=AttemptRequest(
            decision_id=start.decision_id,
            attempt_id=start.attempt_id,
            item_id=start.item_id,
            generation=0,
            admission_charge=1,
        )
    )
    result = trace_step(
        state,
        DecisionSubmitted(decision=start, expected_revision=0),
        ReducerTrace(
            frames=(TraceFrame(signal=signal, change=SchedulingChange(state=state.scheduling)),)
        ),
    )
    return result, start


def test_queued_start_dependency_survives_without_initial_requests() -> None:
    codec, state = operation_state()
    queued, start = queued_start(state)
    assert isinstance(queued.events[0], Accepted)
    assert queued.events[0].request_ids == ()
    operation = codec.validate_decision(
        Operation(
            decision_id=DecisionId(root="dependent"),
            scope=start.scope,
            depends_on=(start.decision_id,),
            request=ArtifactPut(content="after attempt"),
            deadline_at=100.0,
        )
    )
    trace = (
        core.operation_trace(queued.state, operation)
        if hasattr(core, "operation_trace")
        else ReducerTrace(frames=())
    )
    dependent = trace_step(
        queued.state, DecisionSubmitted(decision=operation, expected_revision=1), trace
    )
    assert getattr(dependent.requests[0], "decision_dependencies", ()) == (start.decision_id,)
    assert isinstance(dependent.events[0], core.Accepted)
    assert dependent.events[0].dependencies == (core.DependencyRef(decision_id=start.decision_id),)
    assert (
        core.dependency_status(dependent.state, dependent.requests[0])
        == core.DependencyStatus.PENDING
    )
    identity = dependent.requests[0].request_id
    assert identity is not None
    with pytest.raises(core.ContractError, match="dependency"):
        trace_step(
            dependent.state,
            core.DispatchAuthorized(request_id=identity),
            ReducerTrace(frames=()),
        )
    completion = core.DecisionCompleted(
        decision_id=start.decision_id, status=core.CompletionStatus.SUCCEEDED
    )
    event = ClockAdvanced(now_at=11.0)
    resolved = core.DecisionDependencyResolved(
        decision_id=start.decision_id, status=core.CompletionStatus.SUCCEEDED
    )
    completed = trace_step(
        dependent.state,
        event,
        ReducerTrace(
            frames=(
                TraceFrame(
                    signal=event,
                    change=SchedulingChange(
                        state=dependent.state.scheduling, signals=(completion,)
                    ),
                ),
                TraceFrame(
                    signal=resolved, change=core.IntentsChange(state=dependent.state.intents)
                ),
            )
        ),
    )
    assert (
        core.dependency_status(completed.state, dependent.requests[0])
        == core.DependencyStatus.SUCCEEDED
    )


def test_later_admission_requests_keep_the_queued_decision_owner() -> None:
    codec, state = operation_state()
    queued, start = queued_start(state)
    request = core.EnsureWorkspace(
        scope=start.scope,
        deadline_at=100.0,
        attempt=core.AttemptRef(attempt_id=start.attempt_id, generation=0),
        plan=start.workspace,
    )
    admission = core.AdmitAttempt(
        request=core.AttemptRequest(
            decision_id=start.decision_id,
            attempt_id=start.attempt_id,
            item_id=start.item_id,
            generation=0,
            admission_charge=1,
        )
    )
    admitted = core.AttemptAdmitted(
        request=admission.request, workspace=start.workspace, budget=start.budget
    )
    event = ClockAdvanced(now_at=10.0)
    result = trace_step(
        queued.state,
        event,
        ReducerTrace(
            frames=(
                TraceFrame(
                    signal=event,
                    change=SchedulingChange(state=queued.state.scheduling, signals=(admission,)),
                ),
                TraceFrame(
                    signal=admitted,
                    change=core.AttemptsChange(state=queued.state.attempts, requests=(request,)),
                ),
            )
        ),
    )
    assert getattr(result.requests[0], "decision_id", None) == start.decision_id
    assert result.state.run.receipts[0].request_ids == (result.requests[0].request_id,)
    del codec


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_new_dependent_rejects_failed_or_cancelled_completion(status: str) -> None:
    codec, state = operation_state()
    queued, start = queued_start(state)
    receipt = queued.state.run.receipts[0].model_copy(
        update={"completion": core.CompletionStatus(status)}
    )
    state = queued.state.model_copy(
        update={"run": queued.state.run.model_copy(update={"receipts": (receipt,)})}
    )
    decision = codec.validate_decision(
        Operation(
            decision_id=DecisionId(root="dependent"),
            scope=start.scope,
            depends_on=(start.decision_id,),
            request=ArtifactPut(content="dependent"),
            deadline_at=100.0,
        )
    )
    result = core.step(
        state, DecisionSubmitted(decision=decision, expected_revision=state.revision)
    )
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].code == core.RejectionCode.DEPENDENCY
    assert result.requests == ()
    assert result.state.intents == state.intents


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_completion_rejects_already_prepared_dependent_and_notifies_intents(
    status: str,
) -> None:
    codec, state = operation_state()
    queued, start = queued_start(state)
    decision = codec.validate_decision(
        Operation(
            decision_id=DecisionId(root="dependent"),
            scope=start.scope,
            depends_on=(start.decision_id,),
            request=ArtifactPut(content="dependent"),
            deadline_at=100.0,
        )
    )
    dependent = trace_step(
        queued.state,
        DecisionSubmitted(decision=decision, expected_revision=1),
        core.operation_trace(queued.state, decision),
    )
    completion = core.DecisionCompleted(
        decision_id=start.decision_id, status=core.CompletionStatus(status)
    )
    notification = core.DecisionDependencyResolved(
        decision_id=start.decision_id, status=completion.status
    )
    event = ClockAdvanced(now_at=10.0)
    result = trace_step(
        dependent.state,
        event,
        ReducerTrace(
            frames=(
                TraceFrame(
                    signal=event,
                    change=SchedulingChange(
                        state=dependent.state.scheduling, signals=(completion,)
                    ),
                ),
                TraceFrame(
                    signal=notification, change=core.IntentsChange(state=dependent.state.intents)
                ),
            )
        ),
    )
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].decision_id == decision.decision_id
    assert isinstance(result.state.run.receipts[1].feedback, core.Rejected)
    assert (
        core.dependency_status(result.state, dependent.requests[0]) == core.DependencyStatus.FAILED
    )
