"""Request dependencies and the first accepted Finish retain exact durable authority."""

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
    facts: tuple[core.RequestBase, core.DecisionReceipt, core.RequestRecord],
) -> None:
    request, _, record = facts
    records = (record,)
    match variant:
        case "request":
            records = ()
        case "duplicate":
            records = (record, record)
        case "observation":
            record = record.model_copy(update={"observation": None})
        case "pending":
            record = record.model_copy(update={"phase": core.RequestPhase.DISPATCHED})
        case _:
            record = _dependency_observation(variant, record)
    if variant not in ("request", "duplicate"):
        records = (record,)
    assert dependencies_for(request, core.RecordsState(records=records)) == (
        Proven(request) if denial is None else denial
    )


def _dependency_observation(variant: str, record: core.RequestRecord) -> core.RequestRecord:
    updates = {
        "episode": {"admission_id": None},
        "failed": {"status": core.ObservationStatus.FAILED},
        "nonterminal": {"terminal": False},
        "not_accepted": {"accepted": False},
    }.get(variant)
    if updates is None:
        return record
    assert record.observation is not None
    return record.model_copy(update={"observation": record.observation.model_copy(update=updates)})


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
    facts: tuple[core.RunState, core.Finish],
) -> None:
    run, expected = facts
    run = _stop_variant(variant, run, expected)
    assert committed_stop(run) == (Proven(expected) if denial is None else denial)


def _stop_variant(variant: str, run: core.RunState, expected: core.Finish) -> core.RunState:
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


def _stop_receipt_variant(variant: str, run: core.RunState, expected: core.Finish) -> core.RunState:
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
    "phase", [core.RequestPhase.PREPARED, core.RequestPhase.DISPATCHED, core.RequestPhase.COMPLETED]
)
@given(dependency_facts())
def test_inflight_dependencies_wait_without_an_observation(
    phase: core.RequestPhase,
    facts: tuple[core.RequestBase, core.DecisionReceipt, core.RequestRecord],
) -> None:
    request, _, record = facts
    record = record.model_copy(update={"phase": phase, "observation": None})
    verdict = dependencies_for(request, core.RecordsState(records=(record,)))
    reason = (
        ProofReason.ABSENT_OBSERVATION
        if phase == core.RequestPhase.COMPLETED
        else ProofReason.UNRESOLVED
    )
    assert verdict == Missing(reason)
