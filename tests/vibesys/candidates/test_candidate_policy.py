"""Settlement and winner choice are decided from evidence and metric order alone."""

from __future__ import annotations

from dataclasses import dataclass

from hypothesis import given, settings
from hypothesis import strategies as st

from vibesys.candidates.api import (
    EligibleRound,
    Gates,
    Unranked,
    best_by,
    choose_winner,
    improves_on,
    settle_for,
)
from vibesys.hypothesis import HypothesisOutcome
from vibesys.metrics import AcceptedReading, Measurement, MetricRow, MetricSpace, Objective
from vs_core.api import (
    EvidenceId,
    EvidenceKind,
    InvocationRef,
    RequestId,
    RevisionId,
    RevisionRef,
    SettlementId,
)
from vs_sim.api.testing import examples

SPACE = MetricSpace(objectives=(Objective(name="tps", direction="max"),))
BOTH = Gates(accuracy=True, benchmark=True)
NEVER_UNSETTLED: frozenset[HypothesisOutcome] = frozenset()
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


@settings(max_examples=examples(pr=8, nightly=100))
@given(accuracy=_STATE, benchmark=_STATE, review=_STATE, failed=st.booleans())
def test_a_candidate_is_winner_eligible_only_when_every_configured_gate_passed(
    *, accuracy: bool | None, benchmark: bool | None, review: bool | None, failed: bool
) -> None:
    attempt = FinishedFake(
        readings=_readings(accuracy=accuracy, benchmark=benchmark),
        review_passed=review,
        failure="boom" if failed else None,
    )
    settle = settle_for(attempt, gates=BOTH, beats_input=True, unsettled=NEVER_UNSETTLED)
    expected = accuracy is True and benchmark is True and review is not False and not failed
    assert settle.eligible is expected
    # A candidate whose accuracy passed stays a buildable parent even if the benchmark failed.
    assert (settle.retention == "candidate") is (accuracy is True)
    assert (settle.candidate is not None) is (accuracy is True)


@settings(max_examples=examples(pr=8, nightly=100))
@given(accuracy=_STATE, benchmark=_STATE)
def test_a_gate_the_run_does_not_configure_is_not_required(
    *, accuracy: bool | None, benchmark: bool | None
) -> None:
    gates = Gates(accuracy=False, benchmark=False)
    attempt = FinishedFake(readings=_readings(accuracy=accuracy, benchmark=benchmark))
    assert settle_for(
        attempt, gates=gates, beats_input=True, unsettled=NEVER_UNSETTLED
    ).eligible is bool(attempt.readings and attempt.candidate)


def test_an_unmeasured_candidate_is_discarded() -> None:
    settle = settle_for(FinishedFake(), gates=BOTH, beats_input=True, unsettled=NEVER_UNSETTLED)
    assert (settle.retention, settle.eligible, settle.candidate) == ("discard", False, None)


@settings(max_examples=examples(pr=8, nightly=100))
@given(baseline=st.integers(1, 100), candidate=st.integers(1, 100))
def test_a_candidate_must_beat_a_measured_input_but_the_search_decides_for_an_unmeasured_one(
    baseline: int, candidate: int
) -> None:
    reading = Measurement("tps", float(candidate), "max")
    input_reading = Measurement("tps", float(baseline), "max")
    beats = improves_on(reading, input_reading, space=SPACE, when_input_unmeasured=False)
    assert beats is (candidate > baseline)
    for answer in (True, False):
        assert improves_on(reading, None, space=SPACE, when_input_unmeasured=answer) is answer
    attempt = FinishedFake(
        readings=(
            _reading(EvidenceKind.CORRECTNESS, passed=True),
            _reading(EvidenceKind.BENCHMARK, passed=True, value=float(candidate)),
        )
    )
    settled = settle_for(attempt, gates=BOTH, beats_input=beats, unsettled=NEVER_UNSETTLED)
    assert settled.eligible is beats


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
    winner = choose_winner((), space=SPACE, baseline=BASELINE, when_unranked=Unranked.NONE)
    assert (winner.settlement, winner.revision, winner.hypothesis_id) == (None, BASELINE, None)


@settings(max_examples=examples(pr=8, nightly=100))
@given(values=st.lists(st.integers(1, 1000), min_size=1, max_size=6, unique=True))
def test_the_best_headline_wins(values: list[int]) -> None:
    rounds = tuple(_round(index + 1, float(value)) for index, value in enumerate(values))
    winner = choose_winner(rounds, space=SPACE, baseline=BASELINE, when_unranked=Unranked.NONE)
    assert winner.hypothesis_id == f"h{values.index(max(values)) + 1}"


@settings(max_examples=examples(pr=8, nightly=100))
@given(count=st.integers(1, 6))
def test_without_benchmark_readings_the_latest_eligible_round_wins(count: int) -> None:
    rounds = tuple(_round(index + 1, None) for index in range(count))
    assert (
        choose_winner(
            rounds, space=SPACE, baseline=BASELINE, when_unranked=Unranked.LATEST
        ).hypothesis_id
        == f"h{count}"
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(count=st.integers(1, 6))
def test_without_readings_the_search_decides_whether_anything_wins(count: int) -> None:
    rounds = tuple(_round(index + 1, None) for index in range(count))
    order = {id(item): item.sequence for item in rounds}.__getitem__
    none = best_by(
        rounds,
        space=SPACE,
        reading=lambda _item: None,
        order=lambda item: order(id(item)),
        when_unranked=Unranked.NONE,
    )
    assert none is None
    assert (
        best_by(
            rounds,
            space=SPACE,
            reading=lambda _item: None,
            order=lambda item: order(id(item)),
            when_unranked=Unranked.LATEST,
        )
        == rounds[-1]
    )
