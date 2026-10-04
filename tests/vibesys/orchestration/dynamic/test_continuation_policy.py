"""Stage-specific repeated failures bound both continued and completed turns."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vibesys.run.dynamic_suspension import repeated_evaluation_failures
from vs_evaluation.api import FailureKind, PartialMeasurement
from vs_runtime.api import (
    AgentEvaluation,
    AgentEvaluationStage,
    AgentEvaluationStageOutcome,
    AgentEvaluationStatus,
)


def _partial(rate: float) -> AgentEvaluation:
    return AgentEvaluation(
        revision="candidate",
        kinds=("accuracy", "benchmark"),
        status=AgentEvaluationStatus.FAILED,
        failure="warmup did not finish",
        stages=(
            AgentEvaluationStage(kind="accuracy", outcome=AgentEvaluationStageOutcome.PASSED),
            AgentEvaluationStage(
                kind="benchmark",
                outcome=AgentEvaluationStageOutcome.FAILED,
                partial_measurement=PartialMeasurement(
                    name="warmup_tokens_per_s",
                    value=rate,
                    unit="tok/s",
                    direction="max",
                ),
            ),
        ),
    )


@given(
    st.lists(st.floats(min_value=64, max_value=127.999, allow_nan=False), min_size=2, max_size=32)
)
def test_partial_rate_plateau_preserves_benchmark_failures_across_accuracy_passes(
    rates: list[float],
) -> None:
    evaluations = [_partial(rate) for rate in rates]
    evaluations.append(
        AgentEvaluation(
            revision="accuracy-only",
            kinds=("accuracy",),
            status=AgentEvaluationStatus.PASSED,
            stages=(
                AgentEvaluationStage(kind="accuracy", outcome=AgentEvaluationStageOutcome.PASSED),
            ),
        )
    )
    repeated = repeated_evaluation_failures(evaluations)
    assert repeated is not None
    assert repeated.kind is FailureKind.MEASUREMENT
    assert repeated.count == len(rates)
    assert "not changed the bottleneck" in repeated.instruction


@given(
    st.lists(st.floats(min_value=64, max_value=127.999, allow_nan=False), min_size=2, max_size=32)
)
def test_benchmark_pass_ends_the_benchmark_failure_history(rates: list[float]) -> None:
    evaluations = [_partial(rate) for rate in rates]
    evaluations.append(
        AgentEvaluation(
            revision="benchmark-passes",
            kinds=("benchmark",),
            status=AgentEvaluationStatus.PASSED,
            stages=(
                AgentEvaluationStage(kind="benchmark", outcome=AgentEvaluationStageOutcome.PASSED),
            ),
        )
    )
    assert repeated_evaluation_failures(evaluations) is None
    assert repeated_evaluation_failures([*evaluations, _partial(70)]) is None


@given(st.integers(min_value=2, max_value=32))
def test_success_resets_tracebacks_without_stage_verdicts(count: int) -> None:
    failure = AgentEvaluation(
        revision="candidate",
        kinds=("benchmark",),
        status=AgentEvaluationStatus.FAILED,
        failure='Traceback (most recent call last):\n  File "candidate.py", line 12, in run\n    warmup()\nRuntimeError: warmup failed',
    )
    success = AgentEvaluation(
        revision="success",
        kinds=("accuracy", "benchmark"),
        status=AgentEvaluationStatus.PASSED,
        stages=(
            AgentEvaluationStage(kind="accuracy", outcome=AgentEvaluationStageOutcome.PASSED),
            AgentEvaluationStage(kind="benchmark", outcome=AgentEvaluationStageOutcome.PASSED),
        ),
    )
    assert repeated_evaluation_failures([failure] * count) is not None
    assert repeated_evaluation_failures([failure] * count + [success, failure]) is None


@given(st.integers(min_value=2, max_value=32))
def test_a_stage_pass_resets_its_history_when_another_stage_failed(count: int) -> None:
    passed_benchmark = AgentEvaluation(
        revision="benchmark-passes",
        kinds=("accuracy", "benchmark"),
        status=AgentEvaluationStatus.FAILED,
        failure="accuracy failed",
        stages=(
            AgentEvaluationStage(kind="accuracy", outcome=AgentEvaluationStageOutcome.FAILED),
            AgentEvaluationStage(kind="benchmark", outcome=AgentEvaluationStageOutcome.PASSED),
        ),
    )
    assert (
        repeated_evaluation_failures([_partial(70)] * count + [passed_benchmark, _partial(70)])
        is None
    )
