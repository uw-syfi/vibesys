"""Historical closure records cannot establish current admission authority."""

import pytest
from hypothesis import given

import vs_core.api as core
from vs_core.api.proofs import Mismatch, Missing, ProofField, ProofReason, Proven, current_closure

from .proof_facts import closure_facts


@pytest.mark.parametrize(
    ("variant", "denial"),
    [
        ("exact", None),
        ("owner", Missing(ProofReason.ABSENT_DECLARATION)),
        ("closure", Missing(ProofReason.ABSENT_REQUEST)),
        ("recorded", Missing(ProofReason.ABSENT_REQUEST)),
        ("episode", Missing(ProofReason.ABSENT_EPISODE)),
        ("authority", Mismatch(ProofField.REQUEST_ID)),
        ("admission", Mismatch(ProofField.ADMISSION_ID)),
        ("historical", Mismatch(ProofField.ADMISSION_ID)),
        ("time", Mismatch(ProofField.PAYLOAD)),
        ("disposition", Mismatch(ProofField.DISPOSITION)),
    ],
)
@given(closure_facts())
def test_current_closure_requires_exact_current_record(
    variant: str,
    denial: Missing | Mismatch | None,
    facts: tuple[core.AttemptView, core.AttemptClosure],
) -> None:
    attempt, closure = facts
    match variant:
        case "owner":
            attempt = None
        case "closure":
            closure = None
        case "recorded":
            attempt = attempt.model_copy(update={"closure": None})
        case "episode":
            attempt = attempt.model_copy(update={"admission_id": None})
        case "historical":
            attempt = attempt.model_copy(update={"admission_id": core.DecisionId(root="reopened")})
        case _:
            closure = _closure_variant(variant, closure)
    assert current_closure(attempt, closure) == (Proven(closure) if denial is None else denial)


def _closure_variant(variant: str, closure: core.AttemptClosure) -> core.AttemptClosure:
    match variant:
        case "authority":
            return closure.model_copy(update={"authority": core.RequestId(root="other")})
        case "admission":
            return closure.model_copy(update={"admission_id": core.DecisionId(root="other")})
        case "time":
            return closure.model_copy(update={"requested_at": closure.requested_at + 1})
        case "disposition":
            return closure.model_copy(
                update={"disposition": "cancel" if closure.disposition == "park" else "park"}
            )
        case _:
            return closure


@given(closure_facts())
def test_closure_presence_and_identity_precedence(
    facts: tuple[core.AttemptView, core.AttemptClosure],
) -> None:
    attempt, closure = facts
    changed = closure.model_copy(
        update={
            "authority": core.RequestId(root="other"),
            "admission_id": core.DecisionId(root="other"),
        }
    )
    assert current_closure(attempt, changed) == Mismatch(ProofField.REQUEST_ID)
    attempt = attempt.model_copy(update={"admission_id": None})
    assert current_closure(attempt, changed) == Missing(ProofReason.ABSENT_EPISODE)
    restored = core.AttemptView.model_validate_json(facts[0].model_dump_json())
    assert current_closure(restored, closure) == Proven(closure)
