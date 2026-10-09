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
    Proven,
    submission_budget_for,
)

from .test_recovery_proof_regressions import _record, _recovering, _resolution, measurement_fixture


@st.composite
def submission_facts(draw: st.DrawFn) -> tuple[core.SubmitMeasurement, core.SubmissionBudget]:
    """Every generated budget is an independent canonical plan normalization."""
    return measurement_fixture(draw(st.text(alphabet="abcxyz", min_size=1, max_size=8)))


@pytest.mark.parametrize(
    "field",
    [
        "exact",
        "absent",
        "scope",
        "owner_scope",
        "stages",
        "request_id",
        "candidate",
        "purpose",
        "evaluator_digest",
        "workload_digest",
        "environment_digest",
        "recipe_digest",
    ],
)
@given(facts=submission_facts())
def test_submission_budget_matches_the_canonical_measurement(
    field: str,
    facts: tuple[core.SubmitMeasurement, core.SubmissionBudget],
) -> None:
    request, budget = facts
    if field == "scope":
        budget = budget.model_copy(
            update={"scope": budget.scope.model_copy(update={"generation": 1})}
        )
    elif field == "owner_scope":
        budget = budget.model_copy(
            update={
                "scope": budget.scope.model_copy(update={"owner": core.AttemptId(root="foreign")})
            }
        )
    elif field == "request_id":
        budget = budget.model_copy(update={"receipts": ()})
    elif field not in ("exact", "absent"):
        wrong = "baseline" if field == "purpose" else "different"
        if field == "candidate":
            wrong = core.RevisionRef(revision_id=core.RevisionId(root="other"), digest="other")
        if field == "stages":
            wrong = (core.MeasurementStageIdentity(stage_id="foreign"),)
        budget = budget.model_copy(
            update={"identity": budget.identity.model_copy(update={field: wrong})}
        )
    budgets = () if field == "absent" else (budget,)
    verdict = submission_budget_for(request, budgets, ())
    if field == "exact":
        assert verdict == Proven(budget)
    elif field in ("absent", "request_id"):
        assert verdict == Missing(ProofReason.ABSENT_RECEIPT)
    else:
        assert verdict == Mismatch(
            ProofField.GENERATION
            if field == "scope"
            else ProofField.SCOPE
            if field == "owner_scope"
            else ProofField.NORMALIZATION
        )
    record = _record(request, core.LifecycleClass.OWNED_JOB)
    state = _recovering(record)
    state = state.model_copy(
        update={"evaluation": core.EvaluationState(submission_budgets=budgets)}
    )
    assert _resolution(state, record.request_id) == (
        "reattached" if field == "exact" else "pending"
    )


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
