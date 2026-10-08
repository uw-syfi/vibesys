"""Whose fault a finished benchmark is: a closed verdict, measured again within a bound."""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_runtime.api import (
    AccuracyEvaluation,
    BenchmarkEvaluation,
    BenchmarkFailureKind,
    CandidateFailed,
    EvaluationPassed,
    InfrastructureFailed,
    SettlingMeasurement,
    verdict_of,
)
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace

_INFRA = BenchmarkFailureKind.INFRASTRUCTURE
_AMBIGUOUS = BenchmarkFailureKind.AMBIGUOUS
_WORKLOAD = BenchmarkFailureKind.WORKLOAD
_MEASURED = BenchmarkEvaluation(executed=True, metric_name="throughput", metric_value=80.0)
_LIMIT = st.integers(min_value=1, max_value=6)


def _failed(kind: BenchmarkFailureKind, note: str = "failed") -> BenchmarkEvaluation:
    return BenchmarkEvaluation(executed=False, feedback=note, failure_kind=kind)


def _measure(*readings: BenchmarkEvaluation, limit: int) -> tuple[object, FakeEvaluation]:
    evaluation = FakeEvaluation()
    evaluation.script_benchmark(*readings)

    async def measure() -> object:
        settling = SettlingMeasurement(limit)
        verdict = None
        while verdict is None:
            verdict = settling.observe(await evaluation.benchmark(FakeWorkspace()))
        return verdict

    return asyncio.run(measure()), evaluation


@given(kind=st.sampled_from(BenchmarkFailureKind), note=st.text(min_size=1))
def test_a_failed_evaluation_is_one_verdict_per_failure_kind(
    kind: BenchmarkFailureKind, note: str
) -> None:
    verdict = verdict_of(_failed(kind, note))

    if kind is _WORKLOAD:
        assert isinstance(verdict, CandidateFailed)
    else:
        assert isinstance(verdict, InfrastructureFailed)
        assert verdict.kind is kind
    assert verdict.feedback == note


def test_a_passed_evaluation_is_a_passed_verdict() -> None:
    assert verdict_of(_MEASURED) == EvaluationPassed(_MEASURED)


@given(kind=st.none() | st.sampled_from(BenchmarkFailureKind), note=st.none() | st.text(min_size=1))
def test_an_evaluation_names_a_failure_kind_exactly_when_it_failed(
    kind: BenchmarkFailureKind | None, note: str | None
) -> None:
    if (kind is None) == (note is None):
        BenchmarkEvaluation(executed=True, feedback=note, failure_kind=kind)
    else:
        with pytest.raises(ValidationError, match="failure kind"):
            BenchmarkEvaluation(executed=True, feedback=note, failure_kind=kind)


@given(limit=_LIMIT)
def test_a_reading_the_candidate_owns_is_never_measured_again(limit: int) -> None:
    verdict, evaluation = _measure(_failed(_WORKLOAD), _MEASURED, limit=limit)

    assert isinstance(verdict, CandidateFailed)
    assert len(evaluation.benchmark_calls) == 1


@given(lost=st.integers(min_value=0, max_value=5), limit=_LIMIT)
def test_an_infrastructure_failure_is_measured_again_until_the_bound(lost: int, limit: int) -> None:
    verdict, evaluation = _measure(*[_failed(_INFRA)] * lost, _MEASURED, limit=limit)

    if lost < limit:
        assert verdict == EvaluationPassed(_MEASURED)
        assert len(evaluation.benchmark_calls) == lost + 1
    else:
        # Outlasting the bound is the machinery's failure, never the candidate's.
        assert isinstance(verdict, InfrastructureFailed)
        assert verdict.kind is _INFRA
        assert len(evaluation.benchmark_calls) == limit


@given(limit=_LIMIT)
def test_an_ambiguous_failure_is_measured_once_more_then_counts_against_the_candidate(
    limit: int,
) -> None:
    verdict, evaluation = _measure(*[_failed(_AMBIGUOUS)] * 8, limit=limit)

    assert isinstance(verdict, CandidateFailed)
    assert len(evaluation.benchmark_calls) == min(limit, 2)


@given(
    readings=st.lists(st.sampled_from((_INFRA, _AMBIGUOUS, _WORKLOAD, None)), max_size=8),
    limit=_LIMIT,
)
def test_measuring_is_bounded_and_ends_in_a_verdict_a_policy_can_act_on(
    readings: list[BenchmarkFailureKind | None], limit: int
) -> None:
    scripted = [_MEASURED if kind is None else _failed(kind) for kind in readings]

    verdict, evaluation = _measure(*scripted, limit=limit)

    assert len(evaluation.benchmark_calls) <= limit
    if isinstance(verdict, InfrastructureFailed):
        assert verdict.kind is _INFRA


@given(kind=st.none() | st.sampled_from(BenchmarkFailureKind), note=st.none() | st.text(min_size=1))
def test_an_accuracy_outcome_names_a_failure_kind_exactly_when_it_failed(
    kind: BenchmarkFailureKind | None, note: str | None
) -> None:
    if (kind is None) == (note is None):
        outcome = AccuracyEvaluation(executed=True, feedback=note, failure_kind=kind)
        assert isinstance(verdict_of(outcome), EvaluationPassed) == (note is None)
    else:
        with pytest.raises(ValidationError, match="failure kind"):
            AccuracyEvaluation(executed=True, feedback=note, failure_kind=kind)
