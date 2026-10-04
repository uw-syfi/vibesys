"""Deadline arithmetic uses supplied time and declared execution stages only."""

from typing import Literal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api.infrastructure import TrustedEvaluationPlan

type Stage = Literal["accuracy", "benchmark", "profile", "framework_setup"]
STAGES: tuple[Stage, ...] = ("accuracy", "benchmark", "profile", "framework_setup")


@given(
    st.lists(st.sampled_from(STAGES), min_size=1, unique=True),
    st.integers(min_value=1, max_value=100000),
    st.integers(min_value=0, max_value=100000),
)
def test_deadline_sums_only_requested_stage_budgets(
    stages: list[Stage], allowance: int, submitted: int
) -> None:
    budgets = dict(zip(STAGES, (7, 11, 13, 3), strict=True))
    plan = TrustedEvaluationPlan(
        accuracy_timeout_seconds=7,
        benchmark_timeout_seconds=11,
        profile_timeout_seconds=13,
        framework_setup_timeout_seconds=3,
    )
    expected = sum(budgets[stage] for stage in stages)
    assert plan.execution_budget_seconds(tuple(stages)) == expected
    assert plan.suspension_deadline_s(submitted, tuple(stages), allowance) == (
        submitted + allowance + expected
    )


@pytest.mark.parametrize("stage", ["accuracy", "benchmark", "profile"])
def test_missing_declared_budget_is_rejected(stage: Stage) -> None:
    with pytest.raises(ValueError, match=f"stages.{stage}"):
        TrustedEvaluationPlan().execution_budget_seconds((stage,))


@pytest.mark.parametrize("stages", [(), ("accuracy", "accuracy")])
def test_requested_stages_are_nonempty_and_unique(stages: tuple[Stage, ...]) -> None:
    with pytest.raises(ValueError, match="stages"):
        TrustedEvaluationPlan(accuracy_timeout_seconds=1).execution_budget_seconds(stages)


@pytest.mark.parametrize("submitted", [-1, float("nan"), float("inf")])
def test_invalid_submit_time_is_rejected(submitted: float) -> None:
    with pytest.raises(ValueError, match="submitted_at_s"):
        TrustedEvaluationPlan(accuracy_timeout_seconds=1).suspension_deadline_s(
            submitted, ("accuracy",), 900
        )


@pytest.mark.parametrize("allowance", [0, -1, True, 900.0])
def test_invalid_queue_allowance_is_rejected(allowance: int) -> None:
    with pytest.raises(ValueError, match="queue_allowance_seconds"):
        TrustedEvaluationPlan(accuracy_timeout_seconds=1).suspension_deadline_s(
            0, ("accuracy",), allowance
        )


@given(exponent=st.integers(min_value=309, max_value=1000))
@pytest.mark.parametrize("oversized_field", ["queue_allowance", "stage_budget"])
def test_unrepresentable_deadlines_reject_with_named_validation_error(
    exponent: int, oversized_field: str
) -> None:
    oversized = 10**exponent
    plan = TrustedEvaluationPlan(
        accuracy_timeout_seconds=oversized if oversized_field == "stage_budget" else 1
    )
    allowance = oversized if oversized_field == "queue_allowance" else 900
    with pytest.raises(ValueError, match="suspension deadline must be finite"):
        plan.suspension_deadline_s(0.0, ("accuracy",), allowance)
