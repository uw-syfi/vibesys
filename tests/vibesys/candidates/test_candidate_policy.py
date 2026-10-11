"""Settlement and winner choice are decided from evidence and metric order alone."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given
from hypothesis import strategies as st

from vibesys.candidates.api import (
    BaselineStage,
    BaselineState,
    CandidateConfig,
    EligibleRound,
    choose_winner,
    settle_for,
)
from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import AcceptedReading, MetricRow
from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    InvocationRef,
    RequestId,
    RevisionId,
    RevisionRef,
    SettlementId,
)

CANDIDATE = RevisionRef(revision_id=RevisionId(root="c"), digest="dc")
BASELINE = RevisionRef(revision_id=RevisionId(root="b"), digest="db")


@dataclass
class FinishedFake:
    readings: tuple[AcceptedReading, ...] = ()
    candidate: RevisionRef | None = CANDIDATE
    review_passed: bool | None = None
    judge_invocation: InvocationRef | None = None
    outcome: HypothesisOutcome | None = HypothesisOutcome.NOMINATED
    failure: str | None = None


def _reading(kind: EvidenceKind, *, passed: bool, value: float = 1.0) -> AcceptedReading:
    return AcceptedReading(
        evidence_id=EvidenceId(root=kind.value),
        kind=kind,
        passed=passed,
        stage=kind.value,
        metrics=(MetricRow(name="tps", value=value, direction="max"),),
        source_request=RequestId(root="request"),
    )


def _readings(*, accuracy: bool | None, benchmark: bool | None) -> tuple[AcceptedReading, ...]:
    return (
        *(() if accuracy is None else (_reading(EvidenceKind.CORRECTNESS, passed=accuracy),)),
        *(() if benchmark is None else (_reading(EvidenceKind.BENCHMARK, passed=benchmark),)),
    )


_STATE = st.sampled_from([None, True, False])


@given(accuracy=_STATE, benchmark=_STATE, review=_STATE, failed=st.booleans())
def test_a_candidate_is_winner_eligible_only_when_every_configured_gate_passed(
    *, accuracy: bool | None, benchmark: bool | None, review: bool | None, failed: bool
) -> None:
    attempt = FinishedFake(
        readings=_readings(accuracy=accuracy, benchmark=benchmark),
        review_passed=review,
        failure="boom" if failed else None,
    )
    settle = settle_for(attempt, BaselineState(), CandidateConfig())
    expected = accuracy is True and benchmark is True and review is not False and not failed
    assert settle.eligible is expected
    # A candidate whose accuracy passed stays a buildable parent even if the benchmark failed.
    assert (settle.retention == "candidate") is (accuracy is True)
    assert (settle.candidate is not None) is (accuracy is True)


@given(accuracy=_STATE, benchmark=_STATE)
def test_a_gate_the_run_does_not_configure_is_not_required(
    *, accuracy: bool | None, benchmark: bool | None
) -> None:
    config = CandidateConfig(accuracy_configured=False, benchmark_configured=False)
    attempt = FinishedFake(readings=_readings(accuracy=accuracy, benchmark=benchmark))
    assert settle_for(attempt, BaselineState(), config).eligible is bool(
        attempt.readings and attempt.candidate
    )


def test_an_unmeasured_candidate_is_discarded() -> None:
    settle = settle_for(FinishedFake(), BaselineState(), CandidateConfig())
    assert (settle.retention, settle.eligible, settle.candidate) == ("discard", False, None)


@given(baseline=st.integers(1, 100), candidate=st.integers(1, 100))
def test_a_candidate_must_beat_a_measured_baseline_but_not_an_unmeasured_one(
    baseline: int, candidate: int
) -> None:
    measured = BaselineState(
        stage=BaselineStage.MEASURED,
        metrics=(MetricRow(name="tps", value=float(baseline), direction="max"),),
    )
    attempt = FinishedFake(
        readings=(
            _reading(EvidenceKind.CORRECTNESS, passed=True),
            _reading(EvidenceKind.BENCHMARK, passed=True, value=float(candidate)),
        )
    )
    assert settle_for(attempt, measured, CandidateConfig()).eligible is (candidate > baseline)
    assert settle_for(attempt, BaselineState(), CandidateConfig()).eligible


def _round(index: int, value: float | None) -> EligibleRound:
    metrics = () if value is None else (MetricRow(name="tps", value=value, direction="max"),)
    return EligibleRound(
        hypothesis_id=f"h{index}",
        sequence=index,
        settlement=SettlementId(root=f"s{index}"),
        candidate=RevisionRef(revision_id=RevisionId(root=f"c{index}"), digest=f"d{index}"),
        metrics=metrics,
    )


def test_with_no_eligible_round_the_trusted_baseline_wins() -> None:
    winner = choose_winner((), CandidateConfig(), BASELINE)
    assert (winner.settlement, winner.revision, winner.hypothesis_id) == (None, BASELINE, None)


@given(values=st.lists(st.integers(1, 1000), min_size=1, max_size=6, unique=True))
def test_the_best_headline_wins(values: list[int]) -> None:
    rounds = tuple(_round(index + 1, float(value)) for index, value in enumerate(values))
    winner = choose_winner(rounds, CandidateConfig(), BASELINE)
    assert winner.hypothesis_id == f"h{values.index(max(values)) + 1}"


@given(count=st.integers(1, 6))
def test_without_benchmark_readings_the_latest_eligible_round_wins(count: int) -> None:
    rounds = tuple(_round(index + 1, None) for index in range(count))
    assert choose_winner(rounds, CandidateConfig(), BASELINE).hypothesis_id == f"h{count}"
