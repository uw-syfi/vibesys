"""Optional dependencies and the first accepted Stop retain exact durable authority."""

import pytest
from hypothesis import given

import vs_core.api as core
from vs_core.api.proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    committed_stop,
    dependencies_for,
)

from .proof_facts import dependency_facts, fact_digest, stop_facts


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("optional_empty", None),
        ("receipt", Missing(ProofReason.ABSENT_RECEIPT)),
        ("decision", Missing(ProofReason.ABSENT_RECEIPT)),
        ("feedback", Mismatch(ProofField.FEEDBACK_ID)),
        ("duplicate_receipt", Mismatch(ProofField.RECEIPT_ID)),
        ("rejected", Missing(ProofReason.NOT_ACCEPTED)),
        ("pending_decision", Missing(ProofReason.UNRESOLVED)),
        ("failed_decision", Mismatch(ProofField.STATUS)),
        ("digest", Mismatch(ProofField.DIGEST)),
    ],
)
@given(dependency_facts())
def test_decision_dependencies_require_exact_completion(
    variant: str,
    denial: Missing | Mismatch | None,
    facts: tuple[core.RequestBase, core.DecisionReceipt, core.Intent],
) -> None:
    request, receipt, intent = facts
    receipts = (receipt,)
    match variant:
        case "optional_empty":
            request = request.model_copy(update={"decision_dependencies": (), "depends_on": ()})
        case "receipt":
            receipts = ()
        case "duplicate_receipt":
            receipts = (receipt, receipt)
        case _:
            receipt = _dependency_receipt(variant, receipt)
    if variant not in ("receipt", "duplicate_receipt"):
        receipts = (receipt,)
    assert dependencies_for(request, receipts, core.IntentsState(intents=(intent,))) == (
        Proven(request) if denial is None else denial
    )


def _dependency_receipt(variant: str, receipt: core.DecisionReceipt) -> core.DecisionReceipt:
    match variant:
        case "decision":
            return receipt.model_copy(update={"decision": None})
        case "feedback":
            return receipt.model_copy(
                update={"feedback": core.Accepted(decision_id=core.DecisionId(root="other"))}
            )
        case "rejected":
            return receipt.model_copy(
                update={
                    "feedback": core.Rejected(
                        decision_id=receipt.decision_id,
                        code=core.RejectionCode.DEPENDENCY,
                        path=(),
                        detail="denied",
                    )
                }
            )
        case "pending_decision":
            return receipt.model_copy(update={"completion": None})
        case "failed_decision":
            return receipt.model_copy(update={"completion": core.CompletionStatus.FAILED})
        case "digest":
            receipt = receipt.model_copy(update={"payload_digest": "other"})
    return receipt


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("request", Missing(ProofReason.ABSENT_REQUEST)),
        ("duplicate", Mismatch(ProofField.REQUEST_ID)),
        ("observation", Missing(ProofReason.ABSENT_OBSERVATION)),
        ("episode", Missing(ProofReason.ABSENT_EPISODE)),
        ("pending", Missing(ProofReason.UNRESOLVED)),
        ("failed", Mismatch(ProofField.STATUS)),
        ("nonterminal", Mismatch(ProofField.STATUS)),
        ("not_accepted", Mismatch(ProofField.STATUS)),
    ],
)
@given(dependency_facts())
def test_request_dependencies_require_exact_success(
    variant: str,
    denial: Missing | Mismatch | None,
    facts: tuple[core.RequestBase, core.DecisionReceipt, core.Intent],
) -> None:
    request, receipt, intent = facts
    intents = (intent,)
    match variant:
        case "request":
            intents = ()
        case "duplicate":
            intents = (intent, intent)
        case "observation":
            intent = intent.model_copy(update={"observation": None})
        case "pending":
            intent = intent.model_copy(update={"phase": core.IntentPhase.DISPATCHED})
        case _:
            intent = _dependency_observation(variant, intent)
    if variant not in ("request", "duplicate"):
        intents = (intent,)
    assert dependencies_for(request, (receipt,), core.IntentsState(intents=intents)) == (
        Proven(request) if denial is None else denial
    )


def _dependency_observation(variant: str, intent: core.Intent) -> core.Intent:
    updates = {
        "episode": {"admission_id": None},
        "failed": {"status": core.ObservationStatus.FAILED},
        "nonterminal": {"terminal": False},
        "not_accepted": {"accepted": False},
    }.get(variant)
    if updates is None:
        return intent
    assert intent.observation is not None
    return intent.model_copy(update={"observation": intent.observation.model_copy(update=updates)})


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("later_rejected", None),
        ("later_accepted", None),
        ("terminal", None),
        ("receipt", Missing(ProofReason.ABSENT_RECEIPT)),
        ("result", Missing(ProofReason.UNRESOLVED)),
        ("feedback", Mismatch(ProofField.FEEDBACK_ID)),
        ("scope", Mismatch(ProofField.SCOPE)),
        ("generation", Mismatch(ProofField.GENERATION)),
        ("running", Mismatch(ProofField.STATUS)),
        ("disposition", Mismatch(ProofField.DISPOSITION)),
        ("digest", Mismatch(ProofField.DIGEST)),
    ],
)
@given(stop_facts())
def test_committed_stop_selects_first_accepted_command(
    variant: str,
    denial: Missing | Mismatch | None,
    facts: tuple[core.RunState, core.Stop],
) -> None:
    run, expected = facts
    run = _stop_variant(variant, run, expected)
    assert committed_stop(run) == (Proven(expected) if denial is None else denial)


def _stop_variant(variant: str, run: core.RunState, expected: core.Stop) -> core.RunState:
    match variant:
        case "receipt":
            return run.model_copy(update={"receipts": ()})
        case "result":
            return run.model_copy(update={"result": None})
        case "running":
            return run.model_copy(update={"status": core.RunStatus.RUNNING})
        case "terminal":
            return run.model_copy(update={"status": core.RunStatus.TERMINAL})
        case "disposition":
            return run.model_copy(
                update={"result": expected.result.model_copy(update={"reason": "other"})}
            )
        case _:
            return _stop_receipt_variant(variant, run, expected)


def _stop_receipt_variant(variant: str, run: core.RunState, expected: core.Stop) -> core.RunState:
    receipt = run.receipts[0]
    match variant:
        case "feedback":
            receipt = receipt.model_copy(
                update={"feedback": core.Accepted(decision_id=core.DecisionId(root="other"))}
            )
        case "digest":
            receipt = receipt.model_copy(update={"payload_digest": "other"})
        case "scope":
            return run.model_copy(update={"run_id": core.RunId(root="other")})
        case "generation":
            return run.model_copy(update={"generation": run.generation + 1})
        case "later_rejected" | "later_accepted":
            later = expected.model_copy(
                update={
                    "decision_id": core.DecisionId(root="later"),
                    "depends_on": (core.DecisionId(root="unresolved"),),
                }
            )
            feedback = (
                core.Accepted(decision_id=later.decision_id)
                if variant == "later_accepted"
                else core.Rejected(
                    decision_id=later.decision_id,
                    code=core.RejectionCode.DEPENDENCY,
                    path=(),
                    detail="denied",
                )
            )
            later_receipt = core.DecisionReceipt(
                decision_id=later.decision_id,
                decision=later,
                payload_digest=fact_digest(later),
                feedback=feedback,
            )
            return run.model_copy(update={"receipts": (*run.receipts, later_receipt)})
    return run.model_copy(update={"receipts": (receipt,)})


@pytest.mark.parametrize(
    "phase", [core.IntentPhase.PREPARED, core.IntentPhase.DISPATCHED, core.IntentPhase.COMPLETED]
)
@given(dependency_facts())
def test_inflight_dependencies_wait_without_an_observation(
    phase: core.IntentPhase,
    facts: tuple[core.RequestBase, core.DecisionReceipt, core.Intent],
) -> None:
    request, receipt, intent = facts
    intent = intent.model_copy(update={"phase": phase, "observation": None})
    verdict = dependencies_for(request, (receipt,), core.IntentsState(intents=(intent,)))
    reason = (
        ProofReason.ABSENT_OBSERVATION
        if phase == core.IntentPhase.COMPLETED
        else ProofReason.UNRESOLVED
    )
    assert verdict == Missing(reason)
    dependent = core.InspectRequest(
        scope=request.scope,
        deadline_at=request.deadline_at,
        target=intent.request_id,
        depends_on=request.depends_on,
        decision_dependencies=request.decision_dependencies,
    )
    state = core.initial_state()
    state = state.model_copy(
        update={
            "run": state.run.model_copy(update={"receipts": (receipt,)}),
            "intents": core.IntentsState(intents=(intent,)),
        }
    )
    status = (
        core.DependencyStatus.FAILED
        if phase == core.IntentPhase.COMPLETED
        else core.DependencyStatus.PENDING
    )
    assert core.dependency_status(state, dependent) == status


@pytest.mark.parametrize("order", ["pending-first", "rejected-first"])
@given(dependency_facts())
def test_a_pending_dependency_cannot_hide_a_rejected_dependency(
    order: str,
    facts: tuple[core.RequestBase, core.DecisionReceipt, core.Intent],
) -> None:
    request, receipt, intent = facts
    assert receipt.decision is not None
    rejected_decision = receipt.decision.model_copy(
        update={"decision_id": core.DecisionId(root="rejected")}
    )
    rejected = core.DecisionReceipt(
        decision_id=rejected_decision.decision_id,
        decision=rejected_decision,
        payload_digest=fact_digest(rejected_decision),
        feedback=core.Rejected(
            decision_id=rejected_decision.decision_id,
            code=core.RejectionCode.DEPENDENCY,
            path=(),
            detail="denied",
        ),
    )
    pending = receipt.model_copy(update={"completion": None})
    ids = (
        (rejected.decision_id, pending.decision_id)
        if order == "rejected-first"
        else (pending.decision_id, rejected.decision_id)
    )
    request = request.model_copy(update={"decision_dependencies": ids})
    assert dependencies_for(
        request, (pending, rejected), core.IntentsState(intents=(intent,))
    ) == Missing(ProofReason.NOT_ACCEPTED)
    dependent = core.InspectRequest(
        scope=request.scope,
        deadline_at=request.deadline_at,
        target=intent.request_id,
        decision_dependencies=ids,
    )
    state = core.initial_state()
    state = state.model_copy(
        update={"run": state.run.model_copy(update={"receipts": (pending, rejected)})}
    )
    assert core.dependency_status(state, dependent) == core.DependencyStatus.FAILED
