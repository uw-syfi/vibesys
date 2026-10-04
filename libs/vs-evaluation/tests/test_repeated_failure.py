"""A repeated failure is one stage failing the same way, with no pass of that stage between."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_evaluation.api import (
    EvaluationOperationSnapshot,
    EvaluationStageOutcome,
    EvaluationState,
    EvidenceKind,
    EvidenceOutcome,
    FailureKind,
    PartialMeasurement,
    classify_failure,
    detect_repeated_failure,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


def _traceback(line: int) -> str:
    return (
        "Traceback (most recent call last):\n"
        f'  File "/stage/engine/model.py", line {line}, in forward\n'
        "    raise ValueError(message)\n"
        "ValueError: capacity exceeded\n"
    )


def _warmup(value: float) -> PartialMeasurement:
    return PartialMeasurement(
        name="warmup_output_tokens_per_s",
        value=value,
        direction="max",
        unit="output tokens/s",
        target=79.7,
    )


# r19's stopped warmups: 7.1 and 7.9 share a range, 12.2 and 16.3 do not.
_RATES = (7.1, 7.9, 12.2, 16.3)
_TEXTS = (None, _traceback(442), _traceback(97), "server exited before readiness")
_STAGE = st.one_of(
    st.none(),
    st.just(EvidenceOutcome.PASSED),
    st.tuples(st.sampled_from(_TEXTS), st.sampled_from((None, *_RATES))),
)


@st.composite
def _snapshots(draw: st.DrawFn) -> EvaluationOperationSnapshot:
    outcomes = []
    for kind in (EvidenceKind.ACCURACY, EvidenceKind.BENCHMARK):
        stage = draw(_STAGE)
        if stage is EvidenceOutcome.PASSED:
            outcomes.append(EvaluationStageOutcome(kind=kind, outcome=EvidenceOutcome.PASSED))
        elif stage is not None:
            tail, rate = stage
            outcomes.append(
                EvaluationStageOutcome(
                    kind=kind,
                    outcome=EvidenceOutcome.FAILED,
                    summary_tail=tail,
                    partial_measurement=None if rate is None else _warmup(rate),
                )
            )
    return EvaluationOperationSnapshot(
        handle_id=f"h{draw(st.integers(0, 9))}",
        state=draw(st.sampled_from(EvaluationState)),
        evidence_recorded=draw(st.booleans()),
        stage_outcomes=tuple(outcomes),
        failure=draw(st.sampled_from(_TEXTS)),
    )


def _finished(snapshot: EvaluationOperationSnapshot) -> bool:
    return snapshot.failure is not None or snapshot.state is EvaluationState.SUCCEEDED


def _passes(snapshot: EvaluationOperationSnapshot, stage: EvidenceKind | None) -> bool:
    if stage is None:
        return classify_failure(snapshot) is None
    return any(
        item.kind is stage and item.outcome is EvidenceOutcome.PASSED
        for item in snapshot.stage_outcomes
    )


def _expected_count(snapshots: Sequence[EvaluationOperationSnapshot]) -> int:
    """Count back from the last failure until its stage passes or fails differently."""
    last = classify_failure(snapshots[-1]) if _finished(snapshots[-1]) else None
    if last is None or last.kind is None:
        return 0
    count = 0
    for snapshot in reversed(snapshots):
        if not _finished(snapshot):
            continue
        signature = classify_failure(snapshot)
        if signature is not None and signature.stage is last.stage:
            if signature != last:
                break
            count += 1
        elif _passes(snapshot, last.stage):
            break
    return count


@settings(max_examples=300, deadline=None)
@given(st.lists(_snapshots(), min_size=1, max_size=8))
def test_repeated_failure_is_raised_exactly_when_a_stage_repeats_its_signature(
    snapshots: list[EvaluationOperationSnapshot],
) -> None:
    expected = _expected_count(snapshots)

    repeated = detect_repeated_failure(snapshots)

    if expected < 2:
        assert repeated is None
    else:
        assert repeated is not None
        assert repeated.count == expected


def _benchmark_stop(rate: float) -> EvaluationOperationSnapshot:
    return EvaluationOperationSnapshot(
        handle_id="h",
        state=EvaluationState.SUCCEEDED,
        evidence_recorded=True,
        stage_outcomes=(
            EvaluationStageOutcome(kind=EvidenceKind.ACCURACY, outcome=EvidenceOutcome.PASSED),
            EvaluationStageOutcome(
                kind=EvidenceKind.BENCHMARK,
                outcome=EvidenceOutcome.FAILED,
                partial_measurement=_warmup(rate),
                summary_tail="warmup sub-run stopped",
            ),
        ),
    )


@pytest.mark.parametrize(("rates", "count"), [((7.1, 7.9), 2), ((7.1, 7.9, 7.4), 3)])
def test_warmups_stopping_in_one_rate_range_repeat_despite_passing_accuracy(
    rates: tuple[float, ...], count: int
) -> None:
    """Regression for r19: ten warmup stops at 7 to 16 tok/s never raised a repeated failure.

    Each evaluation passed accuracy, and a passed evaluation ended every run
    of failures, so the benchmark's repeats were never counted.
    """
    repeated = detect_repeated_failure([_benchmark_stop(rate) for rate in rates])

    assert repeated is not None
    assert (repeated.kind, repeated.stage, repeated.signature, repeated.count) == (
        FailureKind.MEASUREMENT,
        EvidenceKind.BENCHMARK,
        "warmup_output_tokens_per_s in [4, 8) output tokens/s",
        count,
    )


def test_a_warmup_in_a_new_rate_range_is_not_a_repeat() -> None:
    assert detect_repeated_failure([_benchmark_stop(7.1), _benchmark_stop(12.2)]) is None
