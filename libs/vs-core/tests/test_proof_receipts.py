"""Accepted authority requires every canonical receipt identity and payload."""

from dataclasses import FrozenInstanceError
from typing import ClassVar, Literal, cast

import pytest
from hypothesis import given
from pydantic import BaseModel

import vs_core.api as core
from vs_core.api.proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    Proven,
    Verdict,
    accepted_receipt_for,
    nonempty_required,
)

from .proof_facts import receipt_facts, required_facts


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("absent", Missing(ProofReason.ABSENT_RECEIPT)),
        ("canonical", None),
        ("id", Missing(ProofReason.ABSENT_RECEIPT)),
        ("decision", Missing(ProofReason.ABSENT_RECEIPT)),
        ("rejected", Missing(ProofReason.NOT_ACCEPTED)),
        ("duplicate", Mismatch(ProofField.RECEIPT_ID)),
        ("feedback", Mismatch(ProofField.FEEDBACK_ID)),
        ("decision_id", Mismatch(ProofField.DECISION_ID)),
        ("scope", Mismatch(ProofField.SCOPE)),
        ("generation", Mismatch(ProofField.GENERATION)),
        ("payload", Mismatch(ProofField.PAYLOAD)),
        ("digest", Mismatch(ProofField.DIGEST)),
    ],
)
@given(receipt_facts())
def test_accepted_receipt_requires_exact_facts(
    variant: str, denial: Missing | Mismatch | None, facts: tuple[core.Stop, core.DecisionReceipt]
) -> None:
    canonical, receipt = facts
    identity = canonical.decision_id
    rows = (receipt,)
    rows, identity, canonical, receipt = _receipt_variant(variant, canonical, receipt)
    if variant not in ("absent", "duplicate"):
        rows = (receipt,)
    verdict = accepted_receipt_for(rows, identity, canonical)
    assert verdict == (Proven(receipt) if denial is None else denial)
    with pytest.raises(TypeError):
        bool(verdict)
    if canonical is not None and variant == "exact":
        restored = core.DecisionReceipt.model_validate_json(receipt.model_dump_json())
        assert accepted_receipt_for((restored,), identity, canonical) == verdict


@pytest.mark.parametrize(
    ("verdict", "field"),
    [
        (Proven(1), "value"),
        (Missing(ProofReason.UNRESOLVED), "reason"),
        (Mismatch(ProofField.STATUS), "field"),
    ],
)
def test_verdicts_are_frozen_and_explicit(
    verdict: Proven[int] | Missing | Mismatch, field: str
) -> None:
    with pytest.raises(FrozenInstanceError):
        setattr(verdict, field, 2)
    with pytest.raises(TypeError):
        bool(verdict)


@pytest.mark.parametrize("value", ["unresolved", "unknown", ProofField.STATUS])
def test_missing_rejects_raw_and_wrong_enums(value: object) -> None:
    with pytest.raises(TypeError, match="closed enum"):
        Missing(cast("ProofReason", value))


@pytest.mark.parametrize("value", ["status", "unknown", ProofReason.UNRESOLVED])
def test_mismatch_rejects_raw_and_wrong_enums(value: object) -> None:
    with pytest.raises(TypeError, match="closed enum"):
        Mismatch(cast("ProofField", value))


@given(receipt_facts())
def test_nonempty_required_preserves_every_requirement(
    facts: tuple[core.Stop, core.DecisionReceipt],
) -> None:
    _, receipt = facts
    assert nonempty_required(()) == Missing(ProofReason.EMPTY_REQUIRED)
    assert nonempty_required((Proven(receipt),)) == Proven((receipt,))
    denial = Missing(ProofReason.ABSENT_RECEIPT)
    assert nonempty_required((Proven(receipt), denial)) == denial
    assert nonempty_required((denial, Mismatch(ProofField.PAYLOAD))) == denial


@given(receipt_facts())
def test_receipt_failure_precedence(facts: tuple[core.Stop, core.DecisionReceipt]) -> None:
    canonical, receipt = facts
    receipt = receipt.model_copy(
        update={
            "feedback": core.Accepted(decision_id=core.DecisionId(root="other")),
            "payload_digest": "incorrect",
        }
    )
    assert accepted_receipt_for((receipt,), canonical.decision_id, canonical) == Mismatch(
        ProofField.FEEDBACK_ID
    )


def _receipt_variant(
    variant: str, canonical: core.Stop, receipt: core.DecisionReceipt
) -> tuple[
    tuple[core.DecisionReceipt, ...], core.DecisionId | None, core.Stop | None, core.DecisionReceipt
]:
    identity = canonical.decision_id
    rows = (receipt,)
    match variant:
        case "absent":
            rows = ()
        case "canonical":
            return rows, identity, None, receipt
        case "id":
            identity = None
        case "decision":
            receipt = receipt.model_copy(update={"decision": None})
        case "rejected":
            receipt = receipt.model_copy(
                update={
                    "feedback": core.Rejected(
                        decision_id=identity,
                        code=core.RejectionCode.DEPENDENCY,
                        detail="denied",
                        path=(),
                    )
                }
            )
        case "duplicate":
            rows = (receipt, receipt)
        case _:
            receipt = _receipt_identity_variant(variant, canonical, receipt)
    return rows, identity, canonical, receipt


def _receipt_identity_variant(
    variant: str, canonical: core.Stop, receipt: core.DecisionReceipt
) -> core.DecisionReceipt:
    match variant:
        case "feedback":
            receipt = receipt.model_copy(
                update={"feedback": core.Accepted(decision_id=core.DecisionId(root="other"))}
            )
        case "decision_id":
            receipt = receipt.model_copy(
                update={
                    "decision": canonical.model_copy(
                        update={"decision_id": core.DecisionId(root="other")}
                    )
                }
            )
        case "scope":
            receipt = receipt.model_copy(
                update={
                    "decision": canonical.model_copy(
                        update={
                            "scope": canonical.scope.model_copy(
                                update={"owner": core.RunId(root="other")}
                            )
                        }
                    )
                }
            )
        case "generation":
            receipt = receipt.model_copy(
                update={
                    "decision": canonical.model_copy(
                        update={
                            "scope": canonical.scope.model_copy(
                                update={"generation": canonical.scope.generation + 1}
                            )
                        }
                    )
                }
            )
        case "payload":
            receipt = receipt.model_copy(
                update={
                    "decision": canonical.model_copy(
                        update={"mode": "cancel" if canonical.mode == "drain" else "drain"}
                    )
                }
            )
        case "digest":
            receipt = receipt.model_copy(update={"payload_digest": "incorrect"})
    return receipt


class BytesOutcome(core.Value):
    status: Literal["succeeded"] = "succeeded"


class BytesRequest(core.OperationRequest):
    kind: Literal["test.bytes"] = "test.bytes"
    lifecycle: Literal[core.LifecycleClass.QUERY] = core.LifecycleClass.QUERY
    outcome_model: ClassVar[type[BaseModel]] = BytesOutcome
    payload: bytes


@given(receipt_facts())
def test_canonicalization_failure_is_a_payload_verdict(
    facts: tuple[core.Stop, core.DecisionReceipt],
) -> None:
    ingress, receipt = facts
    canonical = core.Operation(
        decision_id=ingress.decision_id,
        scope=ingress.scope,
        deadline_at=100.0,
        request=BytesRequest(payload=b"\xff"),
    )
    receipt = receipt.model_copy(update={"decision": canonical})
    assert accepted_receipt_for((receipt,), canonical.decision_id, canonical) == Mismatch(
        ProofField.PAYLOAD
    )


@given(required_facts())
def test_nonempty_requirement_sets_preserve_first_failure(proofs: tuple[Verdict[int], ...]) -> None:
    verdict = nonempty_required(proofs)
    if not proofs:
        assert verdict == Missing(ProofReason.EMPTY_REQUIRED)
    elif all(isinstance(proof, Proven) for proof in proofs):
        assert isinstance(verdict, Proven)
        assert len(verdict.value) == len(proofs)
        assert tuple(Proven(value) for value in verdict.value) == proofs
    else:
        assert verdict == next(proof for proof in proofs if not isinstance(proof, Proven))
