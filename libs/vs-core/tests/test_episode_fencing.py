"""Admission authority survives separate proposals and fences old mutation."""

from typing import ClassVar, Literal

import pytest
from pydantic import BaseModel

import vs_core.api as core


class WriteOutcome(core.Value):
    wrote: bool


class AttemptWrite(core.OperationRequest):
    kind: Literal["test.attempt.write"] = "test.attempt.write"
    lifecycle: Literal[core.LifecycleClass.IDEMPOTENT_WRITE] = core.LifecycleClass.IDEMPOTENT_WRITE
    outcome_model: ClassVar[type[BaseModel]] = WriteOutcome
    content: str


def attempt_state() -> tuple[core.CoreState, core.Scope]:
    state = core.initial_state()
    owner = core.AttemptView(
        attempt_id=core.AttemptId(root="attempt"),
        item_id=core.ItemId(root="item"),
        generation=0,
        phase=core.AttemptPhase.ACTIVE,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
        admission_id=core.DecisionId(root="admission"),
    )
    return state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))}), core.Scope(
        owner=owner.attempt_id, generation=0
    )


def turn_fixture(scope: core.Scope) -> core.TurnSpec:
    return core.TurnSpec(
        session=core.SessionSpec(
            session_id=core.SessionId(root="session"),
            role_id=core.RoleId(root="role"),
            policy="fresh",
            lifetime="owner",
            access=core.Access.WRITE_CANDIDATE,
        ),
        invocation_id=core.InvocationId(root="invocation"),
        workspace=scope,
        prompts=(),
        output_schema=core.SchemaRef(name="output", version=1),
        deadline_at=100.0,
        charge_class="paid",
    )


def turn_proposal(state: core.CoreState, scope: core.Scope) -> core.Transition:
    turn = turn_fixture(scope)
    decision = core.RequestTurn(decision_id=core.DecisionId(root="turn"), scope=scope, turn=turn)
    signal = core.TurnRequested(scope=scope, turn=turn)
    request = core.DispatchTurn(scope=scope, deadline_at=100.0, turn=turn)
    return core.trace_step(
        state,
        core.DecisionSubmitted(decision=decision, expected_revision=state.revision),
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=signal,
                    change=core.SessionsChange(state=state.sessions, requests=(request,)),
                ),
            )
        ),
    )


def test_separately_proposed_turn_keeps_current_admission_episode() -> None:
    state, scope = attempt_state()
    result = turn_proposal(state, scope)
    assert result.requests[0].admission_id == state.attempts.attempts[0].admission_id


def test_separately_proposed_measurement_keeps_current_admission_episode() -> None:
    state, scope = attempt_state()
    plan = core.MeasurementPlan(
        purpose="official",
        candidate=state.run.facts.baseline,
        evaluator_digest="evaluator",
        workload_digest="workload",
        environment_digest="environment",
        stages=(core.MeasurementStage(stage_id="measurement", execution_budget=10.0),),
        policy="ordered",
        recipe=core.ArtifactRef(artifact_id=core.ArtifactId(root="recipe"), digest="recipe"),
        submitted_at=0.0,
        queue_allowance=0.0,
        deadline_at=10.0,
    )
    decision = core.Measure(decision_id=core.DecisionId(root="measure"), scope=scope, plan=plan)
    signal = core.MeasurementRequested(scope=scope, plan=plan)
    request = core.SubmitMeasurement(scope=scope, deadline_at=10.0, plan=plan)
    result = core.trace_step(
        state,
        core.DecisionSubmitted(decision=decision, expected_revision=0),
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=signal,
                    change=core.EvaluationChange(state=state.evaluation, requests=(request,)),
                ),
            )
        ),
    )
    assert result.requests[0].admission_id == state.attempts.attempts[0].admission_id


def test_separately_proposed_registered_mutation_keeps_current_admission_episode() -> None:
    state, scope = attempt_state()
    descriptor = core.OperationDescriptor(
        kind="test.attempt.write",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="write", version=1),
        outcome_schema=core.SchemaRef(name="wrote", version=1),
    )
    codec = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=AttemptWrite, outcome_model=WriteOutcome
            ),
        )
    )
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
            decision_id=core.DecisionId(root="write"),
            scope=scope,
            deadline_at=100.0,
            request=AttemptWrite(content="content"),
        )
    )
    request = core.ExecuteRegisteredOperation(
        scope=scope,
        deadline_at=100.0,
        operation_id=core.OperationId(root="operation:write"),
        operation=codec.encode(decision.request),
        retry_limit=0,
    )
    signal = core.RequestPrepared(request=request, lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE)
    result = core.trace_step(
        state,
        core.DecisionSubmitted(decision=decision, expected_revision=0),
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=signal,
                    change=core.IntentsChange(state=state.intents, requests=(request,)),
                ),
            )
        ),
    )
    assert result.requests[0].admission_id == state.attempts.attempts[0].admission_id


def test_prepared_mutation_cannot_dispatch_after_a_new_admission_episode() -> None:
    state, scope = attempt_state()
    prepared = turn_proposal(state, scope)
    old_request = prepared.requests[0]
    assert old_request.request_id is not None
    changed = prepared.state.attempts.attempts[0].model_copy(
        update={"admission_id": core.DecisionId(root="new-admission")}
    )
    state = prepared.state.model_copy(update={"attempts": core.AttemptsState(attempts=(changed,))})
    with pytest.raises(core.ContractError, match="admission"):
        core.step(state, core.DispatchAuthorized(request_id=old_request.request_id))


def retirement_state() -> tuple[core.CoreState, core.Scope, core.Withdraw]:
    state, scope = attempt_state()
    owner = state.attempts.attempts[0]
    assert owner.admission_id is not None
    start = core.StartAttempt(
        decision_id=owner.admission_id,
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=owner.attempt_id,
        item_id=owner.item_id,
        workspace=owner.workspace,
        budget=owner.budget,
    )
    withdraw = core.Withdraw(
        decision_id=core.DecisionId(root="withdraw"),
        scope=scope,
        target=core.AttemptRef(attempt_id=owner.attempt_id, generation=0),
        disposition=core.Cancel(),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": tuple(
                        core.DecisionReceipt(
                            decision_id=decision.decision_id,
                            decision=decision,
                            payload_digest="accepted",
                            feedback=core.Accepted(decision_id=decision.decision_id),
                        )
                        for decision in (start, withdraw)
                    )
                }
            )
        }
    )
    return state, scope, withdraw


def test_old_recorded_cleanup_remains_dispatchable_after_reentry() -> None:
    state, scope, withdraw = retirement_state()
    request = core.CloseAttemptScope(
        scope=scope,
        deadline_at=100.0,
        attempt=core.AttemptRef(attempt_id=core.AttemptId(root="attempt"), generation=0),
        admission_id=core.DecisionId(root="admission"),
        decision_id=withdraw.decision_id,
    )
    clock = core.ClockAdvanced(now_at=0.0)
    prepared = core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(state=state.scheduling, requests=(request,)),
                ),
            )
        ),
    )
    assert prepared.requests[0].request_id is not None
    changed = prepared.state.attempts.attempts[0].model_copy(
        update={"admission_id": core.DecisionId(root="new-admission")}
    )
    state = prepared.state.model_copy(update={"attempts": core.AttemptsState(attempts=(changed,))})
    with pytest.raises(core.KernelNotImplementedError):
        core.step(state, core.DispatchAuthorized(request_id=prepared.requests[0].request_id))


@pytest.mark.parametrize("kind", ["snapshot", "retain", "discard"])
@pytest.mark.parametrize("fence", ["episode", "recovery"])
def test_ordinary_retention_and_discard_cannot_bypass_mutation_fences(
    kind: str, fence: str
) -> None:
    state, scope = attempt_state()
    assert isinstance(scope.owner, core.AttemptId)
    attempt = core.AttemptRef(attempt_id=scope.owner, generation=scope.generation)
    constructors = {
        "snapshot": core.SnapshotAndRetain(
            scope=scope, deadline_at=100.0, attempt=attempt, retention="wip"
        ),
        "retain": core.RetainRevision(
            scope=scope,
            deadline_at=100.0,
            attempt=attempt,
            revision=state.run.facts.baseline,
            retention="wip",
        ),
        "discard": core.DiscardWorkspace(scope=scope, deadline_at=100.0, attempt=attempt),
    }
    request = constructors[kind].model_copy(
        update={"admission_id": core.DecisionId(root="admission")}
    )
    clock = core.ClockAdvanced(now_at=0.0)
    prepared = core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(state=state.scheduling, requests=(request,)),
                ),
            )
        ),
    )
    state = prepared.state
    if fence == "episode":
        state = state.model_copy(
            update={
                "attempts": core.AttemptsState(
                    attempts=(
                        state.attempts.attempts[0].model_copy(
                            update={"admission_id": core.DecisionId(root="new")}
                        ),
                    )
                )
            }
        )
    else:
        state = state.model_copy(
            update={
                "intents": state.intents.model_copy(update={"recovery": core.RecoveryBarrier()})
            }
        )
    identity = prepared.requests[0].request_id
    assert identity is not None
    with pytest.raises(core.ContractError, match=r"admission|recovery"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


@pytest.mark.parametrize("bridge", [core.RegisterAttempt, core.AdmitAttempt])
@pytest.mark.parametrize("field", ["attempt_id", "item_id", "generation", "admission_charge"])
def test_initial_admission_requires_exact_canonical_start(
    bridge: type[core.RegisterAttempt] | type[core.AdmitAttempt], field: str
) -> None:
    state = core.initial_state()
    decision = core.StartAttempt(
        decision_id=core.DecisionId(root="start"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=core.AttemptId(root="attempt"),
        item_id=core.ItemId(root="item"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    canonical = core.AttemptRequest(
        decision_id=decision.decision_id,
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=0,
        admission_charge=decision.budget.admission_charge,
    )
    corrupted = canonical.model_copy(
        update={
            field: {
                "attempt_id": core.AttemptId(root="other"),
                "item_id": core.ItemId(root="other"),
                "generation": 1,
                "admission_charge": decision.budget.admission_charge + 1,
            }[field]
        }
    )
    signal = core.AttemptRequested(request=canonical)
    with pytest.raises(core.ContractError, match=field):
        core.trace_step(
            state,
            core.DecisionSubmitted(decision=decision, expected_revision=0),
            core.ReducerTrace(
                frames=(
                    core.TraceFrame(
                        signal=signal,
                        change=core.SchedulingChange(
                            state=state.scheduling, signals=(bridge(request=corrupted),)
                        ),
                    ),
                )
            ),
        )


def test_previous_cleanup_cannot_close_a_reused_session_in_a_new_episode() -> None:
    state, scope, withdraw = retirement_state()
    spec = turn_fixture(scope).session
    state = state.model_copy(
        update={
            "sessions": core.SessionsState(
                sessions=(
                    core.SessionView(
                        spec=spec,
                        scope=scope,
                        generation=1,
                        phase=core.SessionPhase.IDLE,
                        resource_id=core.ResourceId(root="reused-session"),
                    ),
                )
            )
        }
    )
    request = core.CloseSession(
        scope=scope,
        deadline_at=100.0,
        session_id=spec.session_id,
        admission_id=core.DecisionId(root="admission"),
        decision_id=withdraw.decision_id,
    )
    clock = core.ClockAdvanced(now_at=0.0)
    prepared = core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(state=state.scheduling, requests=(request,)),
                ),
            )
        ),
    )
    state = prepared.state.model_copy(
        update={
            "attempts": core.AttemptsState(
                attempts=(
                    prepared.state.attempts.attempts[0].model_copy(
                        update={"admission_id": core.DecisionId(root="new-admission")}
                    ),
                )
            )
        }
    )
    identity = prepared.requests[0].request_id
    assert identity is not None
    with pytest.raises(core.ContractError, match="admission"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


def test_cleanup_cause_for_another_attempt_cannot_authorize_snapshot() -> None:
    state, scope, withdraw = retirement_state()
    wrong = withdraw.model_copy(
        update={"target": core.AttemptRef(attempt_id=core.AttemptId(root="other"), generation=0)}
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "receipts": tuple(
                        receipt.model_copy(update={"decision": wrong})
                        if receipt.decision_id == wrong.decision_id
                        else receipt
                        for receipt in state.run.receipts
                    )
                }
            ),
            "intents": state.intents.model_copy(update={"recovery": core.RecoveryBarrier()}),
        }
    )
    assert isinstance(scope.owner, core.AttemptId)
    request = core.SnapshotAndRetain(
        scope=scope,
        deadline_at=100.0,
        attempt=core.AttemptRef(attempt_id=scope.owner, generation=0),
        retention="wip",
        admission_id=core.DecisionId(root="admission"),
        decision_id=withdraw.decision_id,
    )
    clock = core.ClockAdvanced(now_at=0.0)
    prepared = core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(state=state.scheduling, requests=(request,)),
                ),
            )
        ),
    )
    identity = prepared.requests[0].request_id
    assert identity is not None
    with pytest.raises(core.ContractError, match="recovery"):
        core.step(prepared.state, core.DispatchAuthorized(request_id=identity))


@pytest.mark.parametrize("kind", ["snapshot", "discard", "registered"])
def test_recorded_retirement_cannot_mutate_a_reused_workspace_after_reentry(kind: str) -> None:
    state, scope, withdraw = retirement_state()
    assert isinstance(scope.owner, core.AttemptId)
    attempt = core.AttemptRef(attempt_id=scope.owner, generation=scope.generation)
    descriptor = core.OperationDescriptor(
        kind="test.attempt.write",
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        request_schema=core.SchemaRef(name="write", version=1),
        outcome_schema=core.SchemaRef(name="wrote", version=1),
    )
    registry = core.OperationRegistry(
        (
            core.OperationRegistration(
                descriptor=descriptor, request_model=AttemptWrite, outcome_model=WriteOutcome
            ),
        )
    )
    state = state.model_copy(update={"registry": registry.descriptors})
    requests = {
        "snapshot": core.SnapshotAndRetain(
            scope=scope, deadline_at=100.0, attempt=attempt, retention="wip"
        ),
        "discard": core.DiscardWorkspace(scope=scope, deadline_at=100.0, attempt=attempt),
        "registered": core.ExecuteRegisteredOperation(
            scope=scope,
            deadline_at=100.0,
            operation_id=core.OperationId(root="cleanup"),
            operation=registry.encode(AttemptWrite(content="cleanup")),
            retry_limit=0,
        ),
    }
    request = requests[kind].model_copy(
        update={
            "admission_id": core.DecisionId(root="admission"),
            "decision_id": withdraw.decision_id,
        }
    )
    event = core.RecoveryStarted(epoch=0, now_at=0.0)
    prepared = core.trace_step(
        state,
        event,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=event,
                    change=core.IntentsChange(state=state.intents, requests=(request,)),
                ),
            )
        ),
    )
    state = prepared.state.model_copy(
        update={
            "attempts": core.AttemptsState(
                attempts=(
                    prepared.state.attempts.attempts[0].model_copy(
                        update={"admission_id": core.DecisionId(root="new-admission")}
                    ),
                )
            )
        }
    )
    identity = prepared.requests[0].request_id
    assert identity is not None
    with pytest.raises(core.ContractError, match="admission"):
        core.step(state, core.DispatchAuthorized(request_id=identity))


def test_current_episode_cleanup_can_dispatch_during_recovery_and_closing() -> None:
    state, scope, withdraw = retirement_state()
    assert isinstance(scope.owner, core.AttemptId)
    request = core.SnapshotAndRetain(
        scope=scope,
        deadline_at=100.0,
        attempt=core.AttemptRef(attempt_id=scope.owner, generation=0),
        retention="wip",
        admission_id=core.DecisionId(root="admission"),
        decision_id=withdraw.decision_id,
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(
                attempts=(
                    state.attempts.attempts[0].model_copy(
                        update={"phase": core.AttemptPhase.CLOSING}
                    ),
                )
            ),
            "intents": state.intents.model_copy(update={"recovery": core.RecoveryBarrier()}),
        }
    )
    clock = core.ClockAdvanced(now_at=0.0)
    prepared = core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(state=state.scheduling, requests=(request,)),
                ),
            )
        ),
    )
    identity = prepared.requests[0].request_id
    assert identity is not None
    with pytest.raises(core.KernelNotImplementedError):
        core.step(prepared.state, core.DispatchAuthorized(request_id=identity))


@pytest.mark.parametrize(
    "proof", ["dependency", "authority", "absent", "wrong-episode", "reopened", "wrong-scope"]
)
def test_recorded_closure_authorizes_exact_settlement_and_setup_release_work(proof: str) -> None:
    state, scope = attempt_state()
    assert isinstance(scope.owner, core.AttemptId)
    identity = core.RequestId(root="cleanup")
    closure = core.AttemptClosure(
        disposition="settle",
        requested_at=1.0,
        authority=identity if proof == "authority" else core.RequestId(root="retirement"),
        admission_id=core.DecisionId(root="other")
        if proof == "wrong-episode"
        else core.DecisionId(root="admission"),
    )
    dependencies = (
        (core.ReleaseDependency(kind="workspace", identity=identity),)
        if proof in ("dependency", "wrong-episode", "reopened", "wrong-scope")
        else ()
    )
    owner = state.attempts.attempts[0].model_copy(
        update={
            "phase": core.AttemptPhase.CLOSING,
            "closure": closure,
            "release_dependencies": dependencies,
            "admission_id": core.DecisionId(root="new")
            if proof == "reopened"
            else core.DecisionId(root="admission"),
        }
    )
    state = state.model_copy(
        update={
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"recovery": core.RecoveryBarrier()}),
        }
    )
    request_scope = scope.model_copy(update={"generation": 1}) if proof == "wrong-scope" else scope
    request = core.SnapshotAndRetain(
        request_id=identity,
        scope=request_scope,
        deadline_at=100.0,
        attempt=core.AttemptRef(attempt_id=scope.owner, generation=request_scope.generation),
        retention="candidate",
        admission_id=core.DecisionId(root="admission"),
    )
    clock = core.ClockAdvanced(now_at=0.0)
    prepared = core.trace_step(
        state,
        clock,
        core.ReducerTrace(
            frames=(
                core.TraceFrame(
                    signal=clock,
                    change=core.SchedulingChange(state=state.scheduling, requests=(request,)),
                ),
            )
        ),
    )
    if proof in ("dependency", "authority"):
        with pytest.raises(core.KernelNotImplementedError):
            core.step(prepared.state, core.DispatchAuthorized(request_id=identity))
    else:
        with pytest.raises(core.ContractError, match="recovery"):
            core.step(prepared.state, core.DispatchAuthorized(request_id=identity))
