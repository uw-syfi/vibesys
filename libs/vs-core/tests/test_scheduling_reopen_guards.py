"""Declared reopen authority is required at enqueue and capacity admission."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest
from .reopen_facts import with_reopening_continuation


def _normalize(request: core.OperationRequest) -> core.ScopeReopenNormalization:
    assert isinstance(request, core.ScopedAdmissionReopen)
    return core.ScopeReopenNormalization(
        attempt=request.attempt,
        continuation_id=request.continuation_id,
        park_authority=request.park_authority,
        resolved_cancelled_jobs=request.resolved_cancelled_jobs,
    )


def _fixture(
    index: int, generation: int
) -> tuple[core.CoreState, core.OperationRegistry, core.AttemptReopenRequest]:
    state = core.initial_state()
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
                normalize_scope_reopen=_normalize,
            ),
        )
    )
    attempt = core.AttemptRef(
        attempt_id=core.AttemptId(root=f"attempt-{index}"), generation=generation
    )
    park = core.RequestId(root=f"park-{index}")
    owner = core.AttemptView(
        attempt_id=attempt.attempt_id,
        item_id=core.ItemId(root=f"item-{index}"),
        generation=generation,
        phase=core.AttemptPhase.PARKED,
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(admission_charge=1),
        admission_id=core.DecisionId(root=f"start-{index}"),
        closure=core.AttemptClosure(
            disposition="park",
            requested_at=0.0,
            authority=park,
            admission_id=core.DecisionId(root=f"start-{index}"),
        ),
    )
    decision = codec.validate_decision(
        core.Operation(
            decision_id=core.DecisionId(root=f"reopen-{index}"),
            scope=core.Scope(owner=state.run.run_id, generation=state.run.generation),
            deadline_at=100.0,
            request=core.ScopedAdmissionReopen(
                attempt=attempt,
                continuation_id=core.ContinuationId(root=f"continuation-{index}"),
                park_authority=park,
                resolved_cancelled_jobs=(),
            ),
        )
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    state = state.model_copy(
        update={
            "registry": codec.descriptors,
            "run": state.run.model_copy(
                update={
                    "deadline_at": 100.0,
                    "capabilities": core.Capabilities(operations=codec.descriptors),
                    "receipts": (receipt,),
                }
            ),
            "attempts": core.AttemptsState(attempts=(owner,)),
        }
    )
    state = with_reopening_continuation(
        state,
        owner,
        continuation_id=core.ContinuationId(root=f"continuation-{index}"),
        reopen_authority=core.RequestId(root=f"operation:{decision.decision_id.root}"),
    )
    request = core.AttemptReopenRequest(
        decision_id=decision.decision_id,
        request_id=core.RequestId(root=f"operation:{decision.decision_id.root}"),
        attempt=attempt,
    )
    return state, codec, request


def _step(
    state: core.CoreState, event: core.CoreEvent, codec: core.OperationRegistry
) -> core.Transition:
    before = state.model_dump_json()
    restored = core.CoreState.model_validate_json(before, context={"operation_registry": codec})
    result = core.step(state, event)
    assert result == core.step(restored, event)
    assert state.model_dump_json() == before
    assert (
        core.CoreState.model_validate_json(
            result.state.model_dump_json(), context={"operation_registry": codec}
        )
        == result.state
    )
    return result


@pytest.mark.parametrize(
    "declaration",
    ["absent", "normalization", "kind", "request-schema", "outcome-schema", "version", "lifecycle"],
)
@given(index=st.integers(0, 1000), generation=st.integers(0, 10))
def test_reopen_cannot_allocate_without_exact_run_descriptor(
    declaration: str, index: int, generation: int
) -> None:
    state, codec, request = _fixture(index, generation)
    descriptor = codec.descriptors[0]
    declarations: tuple[core.OperationDescriptor, ...]
    if declaration == "absent":
        declarations = ()
    else:
        changes = {
            "normalization": {"normalization": core.OperationNormalizationKind.NONE},
            "kind": {"kind": "another.operation"},
            "request-schema": {"request_schema": core.SchemaRef(name="different", version=1)},
            "outcome-schema": {"outcome_schema": core.SchemaRef(name="different", version=1)},
            "version": {"request_schema": core.SchemaRef(name="scope-reopen", version=2)},
            "lifecycle": {
                "lifecycle": core.LifecycleClass.QUERY,
                "normalization": core.OperationNormalizationKind.NONE,
            },
        }
        declarations = (descriptor.model_copy(update=changes[declaration]),)
    state = state.model_copy(
        update={
            "registry": (),
            "run": state.run.model_copy(
                update={"capabilities": core.Capabilities(operations=declarations)}
            ),
            "scheduling": core.SchedulingState(queue=(request,)),
        }
    )
    result = _step(state, core.ClockAdvanced(now_at=1.0), codec)
    assert result.state.scheduling.queue == (request,)
    assert result.state.scheduling.slots == ()
    assert result.events == result.requests == ()
    assert result.state.attempts == state.attempts


def _unproved_owner(owner: core.AttemptView, proof: str, index: int) -> core.AttemptView:
    assert owner.closure is not None
    changes = {
        "closure": {"closure": None},
        "park-authority": {
            "closure": owner.closure.model_copy(
                update={"authority": core.RequestId(root=f"foreign-{index}")}
            )
        },
        "disposition": {"closure": owner.closure.model_copy(update={"disposition": "cancel"})},
        "cleanup": {
            "release_dependencies": (
                core.ReleaseDependency(kind="workspace", identity=core.RequestId(root="cleanup")),
            )
        },
    }
    return owner.model_copy(update=changes[proof])


def _successor(state: core.CoreState) -> tuple[core.CoreState, core.AttemptRequest]:
    decision = core.StartAttempt(
        decision_id=core.DecisionId(root="successor"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=core.AttemptId(root="successor"),
        item_id=core.ItemId(root="successor"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(admission_charge=1),
    )
    owner = core.AttemptView(
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=0,
        phase=core.AttemptPhase.QUEUED,
        workspace=decision.workspace,
        budget=decision.budget,
        charges=(
            core.ChargeReceipt(
                charge_id=core.ChargeId(root="successor"), kind=core.ChargeKind.ADMISSION, charged=1
            ),
        ),
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (*state.run.receipts, receipt)}),
            "attempts": core.AttemptsState(attempts=(*state.attempts.attempts, owner)),
        }
    )
    return state, core.AttemptRequest(
        decision_id=decision.decision_id,
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=0,
        admission_charge=1,
    )


@pytest.mark.parametrize(
    "proof",
    [
        "receipt",
        "rejected",
        "decision-identity",
        "feedback-identity",
        "request-identity",
        "target",
        "closure",
        "park-authority",
        "disposition",
        "cleanup",
        "descriptor",
    ],
)
@given(index=st.integers(0, 1000), generation=st.integers(0, 10))
def test_unproved_reopen_is_rejected_before_fifo_persistence(
    proof: str, index: int, generation: int
) -> None:
    state, codec, request = _fixture(index, generation)
    receipt = state.run.receipts[0]
    owner = state.attempts.attempts[0]
    assert owner.closure is not None
    assert isinstance(receipt.decision, core.Operation)
    if proof == "receipt":
        state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": ()})})
    elif proof in ("rejected", "decision-identity", "feedback-identity"):
        changes: dict[str, object]
        foreign = core.DecisionId(root=f"foreign-{index}")
        if proof == "rejected":
            changes = {
                "feedback": core.Rejected(
                    decision_id=request.decision_id,
                    code=core.RejectionCode.OWNERSHIP,
                    path=("proof",),
                    detail="not accepted",
                )
            }
        elif proof == "decision-identity":
            changes = {"decision": receipt.decision.model_copy(update={"decision_id": foreign})}
        else:
            changes = {"feedback": core.Accepted(decision_id=foreign)}
        state = state.model_copy(
            update={
                "run": state.run.model_copy(
                    update={"receipts": (receipt.model_copy(update=changes),)}
                )
            }
        )
    elif proof == "request-identity":
        request = request.model_copy(update={"request_id": core.RequestId(root=f"foreign-{index}")})
    elif proof == "target":
        request = request.model_copy(
            update={"attempt": request.attempt.model_copy(update={"generation": generation + 1})}
        )
    elif proof == "descriptor":
        state = state.model_copy(
            update={"run": state.run.model_copy(update={"capabilities": core.Capabilities()})}
        )
    else:
        state = state.model_copy(
            update={
                "attempts": core.AttemptsState(attempts=(_unproved_owner(owner, proof, index),))
            }
        )
    # No free capacity: malformed requests must be rejected even while execution
    # cannot yet expose them. They must not become the durable FIFO head.
    state = state.model_copy(
        update={
            "scheduling": core.SchedulingState(
                slots=(
                    core.Slot(
                        attempt=core.AttemptRef(
                            attempt_id=core.AttemptId(root="busy"), generation=0
                        ),
                        admission_id=core.DecisionId(root="busy-admission"),
                        admitted_at=0.0,
                    ),
                )
            )
        }
    )
    result = _step(state, core.AttemptReopenRequested(request=request), codec)
    assert result.state.scheduling == state.scheduling
    assert result.requests == ()
    assert len(result.events) == 1
    rejection = result.events[0]
    assert isinstance(rejection, core.Rejected)
    assert rejection.decision_id == request.decision_id
    assert rejection.code == core.RejectionCode.OWNERSHIP
    assert result.state.run.receipts == state.run.receipts
    assert result.state.attempts == state.attempts
    next_state, successor = _successor(result.state)
    next_result = _step(next_state, core.AttemptRequested(request=successor), codec)
    assert next_result.state.scheduling.queue == (successor,)
    assert next_result.state.scheduling.slots == state.scheduling.slots
    assert next_result.events == next_result.requests == ()


@pytest.mark.parametrize("identity", ["decision", "feedback", "run"])
@given(index=st.integers(0, 1000), generation=st.integers(0, 10))
def test_start_rejects_receipts_whose_accepted_identity_is_not_canonical(
    identity: str, index: int, generation: int
) -> None:
    state = core.initial_state()
    decision = core.StartAttempt(
        decision_id=core.DecisionId(root=f"start-{index}"),
        scope=core.Scope(owner=state.run.run_id, generation=generation),
        attempt_id=core.AttemptId(root=f"attempt-{index}"),
        item_id=core.ItemId(root=f"item-{index}"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.ISOLATED_CHILD, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(admission_charge=1),
    )
    foreign = core.DecisionId(root=f"foreign-{index}")
    receipt_decision = decision
    if identity == "decision":
        receipt_decision = decision.model_copy(update={"decision_id": foreign})
    elif identity == "run":
        receipt_decision = decision.model_copy(
            update={
                "scope": core.Scope(
                    owner=core.RunId(root=f"foreign-{index}"), generation=generation
                )
            }
        )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=receipt_decision,
        payload_digest=value_digest(receipt_decision),
        feedback=core.Accepted(
            decision_id=foreign if identity == "feedback" else decision.decision_id
        ),
    )
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": (receipt,)})})
    request = core.AttemptRequest(
        decision_id=decision.decision_id,
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=generation,
        admission_charge=1,
    )
    result = core.step(state, core.AttemptRequested(request=request))
    assert result.state.scheduling == state.scheduling
    assert result.requests == ()
    assert len(result.events) == 1
    assert isinstance(result.events[0], core.Rejected)
    assert result.events[0].code == core.RejectionCode.OWNERSHIP


@pytest.mark.parametrize("proof", ["accepted", "descriptor", "rejected", "closure"])
@given(index=st.integers(0, 1000), generation=st.integers(0, 10), now=st.integers(100, 1000))
def test_accepted_queued_reopen_gets_deadline_retirement(
    proof: str, index: int, generation: int, now: int
) -> None:
    state, codec, request = _fixture(index, generation)
    state = state.model_copy(update={"scheduling": core.SchedulingState(queue=(request,))})
    if proof == "descriptor":
        state = state.model_copy(
            update={"run": state.run.model_copy(update={"capabilities": core.Capabilities()})}
        )
    elif proof == "rejected":
        receipt = state.run.receipts[0].model_copy(
            update={
                "feedback": core.Rejected(
                    decision_id=request.decision_id,
                    code=core.RejectionCode.OWNERSHIP,
                    path=("proof",),
                    detail="not accepted",
                )
            }
        )
        state = state.model_copy(
            update={"run": state.run.model_copy(update={"receipts": (receipt,)})}
        )
    elif proof == "closure":
        owner = state.attempts.attempts[0].model_copy(update={"closure": None})
        state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    if proof != "accepted":
        result = _step(state, core.ClockAdvanced(now_at=float(now)), codec)
        assert result.state.scheduling == state.scheduling
        assert result.state.run.receipts == state.run.receipts
        assert result.state.attempts == state.attempts
        assert result.events == result.requests == ()
        return
    before = state.model_dump_json()
    restored = core.CoreState.model_validate_json(before, context={"operation_registry": codec})
    event = core.ClockAdvanced(now_at=float(now))
    for source in (state, restored):
        failure: core.KernelNotImplementedError | None = None
        result: core.Transition | None = None
        try:
            result = core.step(source, event)
        except core.KernelNotImplementedError as error:
            failure = error
        if failure is not None:
            assert failure.area == core.Area.ATTEMPTS
            assert failure.event_kind == "retire_requested"
        else:
            assert result is not None
            owner = result.state.attempts.attempts[0]
            assert owner.closure is not None
            assert owner.closure.disposition == "cancel"
            assert owner.closure.admission_id == request.decision_id
            # Attempts completes the withdrawn reopen decision as CANCELLED and
            # changes nothing else in the receipts.
            assert (
                tuple(
                    row.model_copy(update={"completion": None}) for row in result.state.run.receipts
                )
                == state.run.receipts
            )
            reopen = next(
                row for row in result.state.run.receipts if row.decision_id == request.decision_id
            )
            assert reopen.completion == core.CompletionStatus.CANCELLED
    assert state.model_dump_json() == before
