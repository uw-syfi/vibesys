"""Kernel admission and queued retirement require canonical accepted identities."""

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core

from .proof_digest import value_digest

type ReceiptVariant = Literal[
    "exact",
    "absent",
    "rejected",
    "feedback",
    "decision",
    "missing_decision",
    "digest",
    "duplicate",
    "wrong_run",
]


def admission_facts(
    identity: int, variant: ReceiptVariant
) -> tuple[core.CoreState, core.StartAttempt, core.AttemptRequest]:
    state = core.initial_state()
    decision = core.StartAttempt(
        decision_id=core.DecisionId(root=f"start:{identity}"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        attempt_id=core.AttemptId(root=f"attempt:{identity}"),
        item_id=core.ItemId(root=f"item:{identity}"),
        workspace=core.WorkspacePlan(
            mode=core.WorkspaceMode.EXCLUSIVE_ROOT, base=state.run.facts.baseline
        ),
        budget=core.AttemptBudget(),
    )
    request = core.AttemptRequest(
        decision_id=decision.decision_id,
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=decision.scope.generation,
        admission_charge=decision.budget.admission_charge,
    )
    receipt = core.DecisionReceipt(
        decision_id=decision.decision_id,
        decision=decision,
        payload_digest=value_digest(decision),
        feedback=core.Accepted(decision_id=decision.decision_id),
    )
    if variant == "rejected":
        receipt = receipt.model_copy(
            update={
                "feedback": core.Rejected(
                    decision_id=decision.decision_id,
                    code=core.RejectionCode.OWNERSHIP,
                    path=("scope",),
                    detail="denied",
                )
            }
        )
    elif variant == "feedback":
        receipt = receipt.model_copy(
            update={"feedback": core.Accepted(decision_id=core.DecisionId(root="foreign"))}
        )
    elif variant in ("decision", "wrong_run"):
        changed = decision.model_copy(
            update={"decision_id": core.DecisionId(root="foreign")}
            if variant == "decision"
            else {"scope": core.Scope(owner=core.RunId(root="foreign-run"), generation=0)}
        )
        receipt = receipt.model_copy(
            update={"decision": changed, "payload_digest": value_digest(changed)}
        )
    elif variant == "missing_decision":
        receipt = receipt.model_copy(update={"decision": None})
    elif variant == "digest":
        receipt = receipt.model_copy(update={"payload_digest": "foreign"})
    receipts = () if variant == "absent" else (receipt,) * (2 if variant == "duplicate" else 1)
    state = state.model_copy(update={"run": state.run.model_copy(update={"receipts": receipts})})
    return state, decision, request


@pytest.mark.parametrize("signal_kind", ["register", "admit"])
@pytest.mark.parametrize(
    "variant",
    [
        "exact",
        "absent",
        "rejected",
        "feedback",
        "decision",
        "missing_decision",
        "digest",
        "duplicate",
        "wrong_run",
    ],
)
@given(identity=st.integers(min_value=0, max_value=10000))
def test_kernel_admission_requires_one_exact_accepted_start(
    signal_kind: Literal["register", "admit"], variant: ReceiptVariant, identity: int
) -> None:
    state, decision, request = admission_facts(identity, variant)
    target = core.AttemptRef(attempt_id=decision.attempt_id, generation=0)
    signal = (
        core.RegisterAttempt(request=request)
        if signal_kind == "register"
        else core.AdmitAttempt(request=request)
    )
    granted = (
        core.AttemptRegistered(
            request=request, workspace=decision.workspace, budget=decision.budget
        )
        if signal_kind == "register"
        else core.AttemptAdmitted(
            request=request,
            admission_id=request.decision_id,
            workspace=decision.workspace,
            budget=decision.budget,
        )
    )
    owner = core.AttemptView(
        attempt_id=decision.attempt_id,
        item_id=decision.item_id,
        generation=0,
        phase=core.AttemptPhase.QUEUED
        if signal_kind == "register"
        else core.AttemptPhase.ACQUIRING,
        admission_id=decision.decision_id,
        workspace=decision.workspace,
        budget=decision.budget,
    )
    scheduling = core.SchedulingState(
        slots=(core.Slot(attempt=target, admission_id=decision.decision_id, admitted_at=1.0),)
        if signal_kind == "admit"
        else ()
    )
    clock = core.ClockAdvanced(now_at=1.0)
    trace = core.ReducerTrace(
        frames=(
            core.TraceFrame(
                signal=clock, change=core.SchedulingChange(state=scheduling, signals=(signal,))
            ),
            core.TraceFrame(
                signal=granted,
                change=core.AttemptsChange(state=core.AttemptsState(attempts=(owner,))),
            ),
        )
    )
    if variant != "exact":
        with pytest.raises(core.ContractError, match="admission"):
            core.trace_step(state, clock, trace)
        assert state.attempts.attempts == ()
    else:
        result = core.trace_step(state, clock, trace)
        assert result.state.attempts.attempts == (owner,)
        assert result.requests == ()
        assert core.CoreState.model_validate_json(result.state.model_dump_json()) == result.state


@pytest.mark.parametrize(
    "variant",
    [
        "exact",
        "absent",
        "rejected",
        "feedback",
        "decision",
        "missing_decision",
        "digest",
        "duplicate",
        "wrong_run",
    ],
)
@given(identity=st.integers(min_value=0, max_value=10000))
def test_queued_retirement_requires_one_canonical_start_registration(
    variant: ReceiptVariant, identity: int
) -> None:
    state, start, _ = admission_facts(identity, variant)
    target = core.AttemptRef(attempt_id=start.attempt_id, generation=0)
    owner = core.AttemptView(
        attempt_id=start.attempt_id,
        item_id=start.item_id,
        generation=0,
        phase=core.AttemptPhase.QUEUED,
        workspace=start.workspace,
        budget=start.budget,
    )
    state = state.model_copy(update={"attempts": core.AttemptsState(attempts=(owner,))})
    withdrawal = core.Withdraw(
        decision_id=core.DecisionId(root=f"withdraw:{identity}"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        target=target,
        disposition=core.Cancel(),
    )
    retirement = core.RetireRequested(
        attempt=target,
        disposition="cancel",
        requested_at=state.run.now_at,
        authority=core.RequestId(root=f"withdraw:{withdrawal.decision_id.root}"),
        admission_id=start.decision_id,
    )
    retired = owner.model_copy(update={"phase": core.AttemptPhase.CLOSING})
    trace = core.ReducerTrace(
        frames=(
            core.TraceFrame(
                signal=retirement,
                change=core.AttemptsChange(state=core.AttemptsState(attempts=(retired,))),
            ),
        )
    )
    event = core.DecisionSubmitted(decision=withdrawal, expected_revision=state.revision)
    if variant != "exact":
        with pytest.raises(core.ContractError, match="admission"):
            core.trace_step(state, event, trace)
        assert state.attempts.attempts == (owner,)
    else:
        result = core.trace_step(state, event, trace)
        assert result.state.attempts.attempts == (retired,)
        assert isinstance(result.events[0], core.Accepted)
        assert result.requests == ()


def cleanup_facts(identity: int, variant: ReceiptVariant) -> tuple[core.CoreState, core.Intent]:
    """Record a canonical Stop cleanup request in its source admission episode."""
    state, start, _ = admission_facts(identity, variant)
    result = core.RunResultProposal(outcome="cancelled", reason="drain")
    stop = core.Stop(
        decision_id=core.DecisionId(root="stop"),
        scope=core.Scope(owner=state.run.run_id, generation=0),
        mode="drain",
        result=result,
    )
    receipt = core.DecisionReceipt(
        decision_id=stop.decision_id,
        decision=stop,
        payload_digest=value_digest(stop),
        feedback=core.Accepted(decision_id=stop.decision_id),
    )
    target = core.AttemptRef(attempt_id=start.attempt_id, generation=0)
    owner = core.AttemptView(
        attempt_id=start.attempt_id,
        item_id=start.item_id,
        generation=0,
        phase=core.AttemptPhase.CLOSING,
        admission_id=start.decision_id,
        workspace=start.workspace,
        budget=start.budget,
    )
    command = core.CloseAttemptScope(
        request_id=core.RequestId(root=f"close:{identity}"),
        scope=core.Scope(owner=start.attempt_id, generation=0),
        attempt=target,
        admission_id=start.decision_id,
        decision_id=stop.decision_id,
        deadline_at=100.0,
    )
    assert command.request_id is not None
    intent = core.Intent(
        request_id=command.request_id,
        request=command,
        payload_digest=value_digest(command),
        lifecycle=core.LifecycleClass.IDEMPOTENT_WRITE,
        phase=core.IntentPhase.PREPARED,
        reconcile_deadline_at=100.0,
    )
    state = state.model_copy(
        update={
            "run": state.run.model_copy(
                update={
                    "status": core.RunStatus.CLOSING,
                    "result": result,
                    "receipts": (*state.run.receipts, receipt),
                }
            ),
            "attempts": core.AttemptsState(attempts=(owner,)),
            "intents": state.intents.model_copy(update={"intents": (intent,)}),
        }
    )
    return state, intent


@pytest.mark.parametrize("variant", ["exact", "wrong_run"])
@given(identity=st.integers(min_value=0, max_value=10000))
def test_stop_cleanup_requires_the_recorded_episode_from_this_run(
    variant: ReceiptVariant, identity: int
) -> None:
    state, intent = cleanup_facts(identity, variant)
    event = core.DispatchAuthorized(request_id=intent.request_id)
    dispatched = intent.model_copy(update={"phase": core.IntentPhase.DISPATCHED})
    trace = core.ReducerTrace(
        frames=(
            core.TraceFrame(
                signal=event,
                change=core.IntentsChange(
                    state=state.intents.model_copy(update={"intents": (dispatched,)})
                ),
            ),
        )
    )
    if variant == "wrong_run":
        with pytest.raises(core.ContractError, match="admission"):
            core.trace_step(state, event, trace)
        assert state.intents.intents == (intent,)
    else:
        transition = core.trace_step(state, event, trace)
        assert transition.state.intents.intents == (dispatched,)
        assert transition.requests == ()


@pytest.mark.parametrize("count", [1, 2])
@given(identity=st.integers(min_value=0, max_value=10000))
def test_dispatch_requires_an_unambiguous_canonical_intent(count: int, identity: int) -> None:
    state, intent = cleanup_facts(identity, "exact")
    state = state.model_copy(
        update={"intents": state.intents.model_copy(update={"intents": (intent,) * count})}
    )
    event = core.DispatchAuthorized(request_id=intent.request_id)
    dispatched = intent.model_copy(update={"phase": core.IntentPhase.DISPATCHED})
    trace = core.ReducerTrace(
        frames=(
            core.TraceFrame(
                signal=event,
                change=core.IntentsChange(
                    state=state.intents.model_copy(update={"intents": (dispatched,) * count})
                ),
            ),
        )
    )
    if count > 1:
        with pytest.raises(core.ContractError, match="ambiguous canonical request identity"):
            core.trace_step(state, event, trace)
        assert state.intents.intents == (intent,) * count
    else:
        result = core.trace_step(state, event, trace)
        assert result.state.intents.intents == (dispatched,)
        assert result.requests == ()
