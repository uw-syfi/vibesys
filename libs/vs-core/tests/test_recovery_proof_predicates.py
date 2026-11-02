"""Canonical budget normalization properties through published proof contracts."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

import vs_core.api as core
from vs_core.api.proofs import (
    Mismatch,
    Missing,
    ProofField,
    ProofReason,
    submission_budget_for,
)

from .test_recovery_proof_regressions import measurement_fixture


@st.composite
def submission_facts(draw: st.DrawFn) -> tuple[core.SubmitMeasurement, core.SubmissionBudget]:
    """Every generated budget is an independent canonical plan normalization."""
    return measurement_fixture(draw(st.text(alphabet="abcxyz", min_size=1, max_size=8)))


@pytest.mark.parametrize("absence", ["request", "request_identity", "snapshot", "normalization"])
@given(facts=submission_facts())
def test_budget_normalization_absence_is_total_and_never_proven(
    absence: str,
    facts: tuple[core.SubmitMeasurement, core.SubmissionBudget],
) -> None:
    request, budget = facts
    expected = Missing(ProofReason.ABSENT_REQUEST)
    if absence == "request":
        request = None
    elif absence == "request_identity":
        request = request.model_copy(update={"request_id": None})
    elif absence == "snapshot":
        request = request.model_copy(
            update={
                "plan": request.plan.model_copy(
                    update={
                        "candidate": core.SnapshotResultRef(
                            request_id=core.RequestId(root="snapshot")
                        ),
                    }
                )
            }
        )
        expected = Missing(ProofReason.ABSENT_CHECKPOINT)
    else:
        request = request.model_copy(
            update={"plan": request.plan.model_copy(update={"evaluator_digest": ""})}
        )
        expected = Mismatch(ProofField.PAYLOAD)
    assert submission_budget_for(request, (budget,), ()) == expected


@given(facts=submission_facts())
def test_duplicate_prepared_budget_ownership_is_ambiguous(
    facts: tuple[core.SubmitMeasurement, core.SubmissionBudget],
) -> None:
    request, budget = facts
    assert submission_budget_for(request, (budget, budget), ()) == Mismatch(ProofField.REQUEST_ID)
