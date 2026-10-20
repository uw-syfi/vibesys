"""What a serial search keeps of its rounds, and what a finished round asserts to core.

The pure folds the two serial searches share: `SerialRun` folds finished rounds in order and says
when the search is over, which kept candidate leads and why it ended without one; `settle` says
what a round's workspace is closed with. Their rules are stated over generated rounds.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.serial_search._host import TERMS

from vibesys.candidates.api import NotConfigured
from vibesys.hypothesis import HypothesisConfig, HypothesisOutcome
from vibesys.metrics import AcceptedReading, MetricRow
from vibesys.serial_search.api import (
    Closed,
    FinishedRound,
    LedgerJson,
    RoundDone,
    SerialRun,
    SerialState,
    accuracy_passed,
    failed_gate,
    settle,
)
from vs_core.api import (
    AttemptId,
    AttemptRef,
    EvidenceId,
    EvidenceKind,
    RequestId,
    RevisionRef,
    Settlement,
    SettlementId,
)
from vs_core.api.search import INPUT, Kept
from vs_sim.api.testing import examples

INPUT_REVISION = RevisionRef.of_git_commit("input")
CANDIDATE = RevisionRef.of_git_commit("candidate")
NO_OUTCOMES: frozenset[HypothesisOutcome] = frozenset()


def run_of(max_rounds: int) -> SerialRun:
    return SerialRun(
        hypotheses=HypothesisConfig(max_rounds=max_rounds),
        input=INPUT_REVISION,
        terms=TERMS,
        baseline=NotConfigured(),
    )


def settlement(name: str, *, eligible: bool) -> Settlement:
    return Settlement.model_validate(
        {
            "settlement_id": SettlementId(root=f"settlement-{name}"),
            "attempt": AttemptRef(attempt_id=AttemptId(root=f"attempt-{name}"), generation=0),
            "candidate": RevisionRef.of_git_commit(f"c-{name}"),
            "assessments": (),
            "eligible": eligible,
            "retention": "candidate",
            "outcome": "succeeded",
        }
    )


def closed(number: int, *, eligible: bool, value: float, passed: bool | None = None) -> Closed:
    return Closed(
        round_number=number,
        hypothesis_id=f"h{number}",
        kept=Kept.of(settlement(str(number), eligible=eligible)),
        passed=eligible if passed is None else passed,
        eligible=eligible,
        metrics=(MetricRow(name="throughput", value=value, direction="max", unit="tokens/s"),),
    )


def done(
    number: int,
    *,
    counted: bool = True,
    kept: Closed | None = None,
    failure: str | None = None,
    stopped: bool = False,
) -> RoundDone:
    return RoundDone(
        number=number,
        ledger=LedgerJson(),
        counted=counted,
        closed=kept,
        checkpoint=None if kept is None or kept.kept is None else kept.kept.revision,
        failure=failure,
        stopped=stopped,
    )


rounds_drawn = st.lists(
    st.tuples(st.booleans(), st.booleans(), st.floats(1, 1000)), min_size=0, max_size=6
)


@settings(max_examples=examples(pr=8, nightly=100))
@given(drawn=rounds_drawn, limit=st.integers(1, 6))
def test_the_run_counts_the_rounds_that_used_one_and_starts_the_next_on_the_best(
    drawn: list[tuple[bool, bool, float]], limit: int
) -> None:
    run = run_of(limit)
    for index, (counted, eligible, value) in enumerate(drawn, start=1):
        run.finished(
            done(index, counted=counted, kept=closed(index, eligible=eligible, value=value)),
            f"flow:round#{index}",
        )
    assert run.counted == sum(counted for counted, _, _ in drawn)
    start = run.start()
    assert start.number == run.counted + 1
    # the next idea starts from the best (the input while no round beat it), never merely from
    # the newest retained checkpoint, and says which round made it
    best = run.leader()
    if isinstance(best, Kept):
        assert start.base == best.revision
        made = start.retained_as(start.base)
        assert made is not None
        assert start.best_round == made.round_number
        assert made.span == f"flow:round#{made.round_number}"
    else:
        assert start.base == INPUT_REVISION
        assert start.best_round is None
        assert start.retained_as(start.base) is None
    # work in progress is what the previous round retained, which a continuation builds on
    newest = None if not drawn else RevisionRef.of_git_commit(f"c-{len(drawn)}")
    assert start.work_in_progress == newest


def test_an_idea_starts_from_the_best_not_from_a_slower_idea_retained_after_it() -> None:
    """Regression: the base followed the latest retained checkpoint, however slow."""
    run = run_of(4)
    run.finished(done(1, kept=closed(1, eligible=True, value=100.0)), "flow:round#1")
    run.finished(done(2, kept=closed(2, eligible=True, value=40.0, passed=True)), "flow:round#2")
    run.finished(done(3, kept=closed(3, eligible=False, value=10.0, passed=False)), "flow:round#3")

    start = run.start()

    assert start.base == RevisionRef.of_git_commit("c-1")
    assert start.best_round == 1
    assert [item.round_number for item in start.passed_ideas()] == [1, 2]
    assert start.work_in_progress == RevisionRef.of_git_commit("c-3")


@settings(max_examples=examples(pr=8, nightly=100))
@given(drawn=rounds_drawn, limit=st.integers(1, 6), stopping=st.booleans())
def test_the_run_is_over_when_stopped_failed_or_out_of_rounds(
    *, drawn: list[tuple[bool, bool, float]], limit: int, stopping: bool
) -> None:
    run = run_of(limit)
    for index, (counted, eligible, value) in enumerate(drawn, start=1):
        run.finished(
            done(index, counted=counted, kept=closed(index, eligible=eligible, value=value)), None
        )
    if stopping or run.counted >= limit:
        assert run.over(stopping=stopping)
    if run.counted < limit and not stopping:
        assert not run.over(stopping=False)


@settings(max_examples=examples(pr=8, nightly=100))
@given(failure=st.text(min_size=1, max_size=8))
def test_a_failed_round_ends_the_run_for_good(failure: str) -> None:
    failed = run_of(5)
    failed.finished(done(1, failure=failure), None)
    assert failed.over(stopping=False)
    assert failed.failure == failure


def test_a_round_the_operator_stopped_ends_the_run_only_while_the_stop_stands() -> None:
    """Regression: a stop was kept in the checkpoint, so a resume that lifted it ended the search."""
    run = run_of(5)
    run.finished(done(1, counted=False, stopped=True), None)
    assert run.over(stopping=True)
    assert not run.over(stopping=False)
    assert run.counted == 0
    assert not run.state().stopped
    saved = SerialState.model_validate_json(run.state().model_dump_json())
    resumed = type(run).resumed(HypothesisConfig(max_rounds=5), INPUT_REVISION, TERMS, saved)
    assert not resumed.over(stopping=False)


def test_a_checkpoint_an_earlier_version_saved_stopped_resumes_into_the_rounds_that_remain() -> (
    None
):
    """Earlier versions ended the search by a flag in the checkpoint; the lift that resumes it clears it."""
    run = run_of(5)
    saved = run.state().model_copy(update={"stopped": True})
    resumed = type(run).resumed(HypothesisConfig(max_rounds=5), INPUT_REVISION, TERMS, saved)
    assert not resumed.over(stopping=False)
    assert resumed.over(stopping=True)


@settings(max_examples=examples(pr=8, nightly=100))
@given(drawn=rounds_drawn)
def test_the_leader_is_the_input_unless_a_kept_round_is_eligible_and_then_one_of_them(
    drawn: list[tuple[bool, bool, float]],
) -> None:
    run = run_of(8)
    for index, (_, eligible, value) in enumerate(drawn, start=1):
        run.finished(done(index, kept=closed(index, eligible=eligible, value=value)), None)
    leader = run.leader()
    eligible_kept = [item.kept for item in run.closed if item.eligible]
    if not eligible_kept:
        assert leader == INPUT
    else:
        assert leader in eligible_kept
        assert run.ending() is None


@settings(max_examples=examples(pr=8, nightly=100))
@given(drawn=rounds_drawn)
def test_the_best_eligible_candidate_leads(drawn: list[tuple[bool, bool, float]]) -> None:
    run = run_of(8)
    for index, (_, eligible, value) in enumerate(drawn, start=1):
        run.finished(done(index, kept=closed(index, eligible=eligible, value=value)), None)
    eligible = [item for item in run.closed if item.eligible]
    leader = run.leader()
    if eligible:
        assert isinstance(leader, Kept)
        best = max(item.metrics[0].value for item in eligible)
        led = next(item for item in eligible if item.kept == leader)
        assert led.metrics[0].value == best


def test_a_run_whose_input_is_trusted_ends_with_the_input_standing_when_nothing_was_eligible() -> (
    None
):
    run = run_of(2)
    run.finished(done(1, kept=closed(1, eligible=False, value=10.0)), None)
    assert run.ending() is None


@settings(max_examples=examples(pr=8, nightly=100))
@given(failure=st.text(min_size=1, max_size=8))
def test_a_failure_with_nothing_eligible_is_named_in_why_the_run_has_no_winner(
    failure: str,
) -> None:
    run = run_of(2)
    run.finished(done(1, failure=failure), None)
    ending = run.ending()
    assert ending is not None
    assert failure in ending.reason


# -- what a round's workspace is closed with ---------------------------------------------------


def reading(kind: EvidenceKind, *, passed: bool, feedback: str = "") -> AcceptedReading:
    return AcceptedReading(
        source_request=RequestId(root=f"request-{kind.value}"),
        evidence_id=EvidenceId(root=f"evidence-{kind.value}"),
        kind=kind,
        passed=passed,
        stage=kind.value,
        metrics=(),
        feedback=feedback,
    )


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    passed=st.booleans(),
    has_candidate=st.booleans(),
    review=st.none() | st.booleans(),
)
def test_a_round_that_failed_is_never_eligible_and_never_keeps_its_candidate_as_adoptable(
    *, passed: bool, has_candidate: bool, review: bool | None
) -> None:
    finished = FinishedRound(
        readings=(),
        candidate=CANDIDATE if has_candidate else None,
        review_passed=review,
        failure="the turn broke",
        passed=passed,
    )
    kept, assessment = settle(
        finished, baseline=NotConfigured(), terms=TERMS, unsettled=NO_OUTCOMES
    )
    assert not assessment.eligible
    assert kept is None or assessment.retention != "adopt"


@settings(max_examples=examples(pr=8, nightly=100))
@given(passed=st.booleans(), review=st.none() | st.booleans())
def test_a_round_without_a_candidate_keeps_nothing(*, passed: bool, review: bool | None) -> None:
    finished = FinishedRound(
        readings=(), candidate=None, review_passed=review, failure=None, passed=passed
    )
    kept, assessment = settle(
        finished, baseline=NotConfigured(), terms=TERMS, unsettled=NO_OUTCOMES
    )
    assert kept is None
    assert not assessment.eligible


@settings(max_examples=examples(pr=8, nightly=100))
@given(passed=st.booleans())
def test_a_candidate_no_trusted_reading_made_buildable_is_kept_but_never_adoptable(
    *,
    passed: bool,
) -> None:
    finished = FinishedRound(
        readings=(), candidate=CANDIDATE, review_passed=passed, failure=None, passed=passed
    )
    kept, assessment = settle(
        finished, baseline=NotConfigured(), terms=TERMS, unsettled=NO_OUTCOMES
    )
    assert kept == CANDIDATE
    assert not assessment.eligible
    assert assessment.retention == ("candidate" if passed else "wip")


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    accuracy=st.none() | st.booleans(),
    benchmark=st.none() | st.booleans(),
    feedback=st.text(min_size=1, max_size=6),
)
def test_the_first_rejecting_gate_names_the_feedback_accuracy_before_benchmark(
    *, accuracy: bool | None, benchmark: bool | None, feedback: str
) -> None:
    readings = tuple(
        reading(kind, passed=ok, feedback=f"{kind.value}:{feedback}")
        for kind, ok in (
            (EvidenceKind.CORRECTNESS, accuracy),
            (EvidenceKind.BENCHMARK, benchmark),
        )
        if ok is not None
    )
    rejected = failed_gate(readings)
    if accuracy is False:
        assert rejected == f"correctness:{feedback}"
    elif benchmark is False:
        assert rejected == f"benchmark:{feedback}"
    else:
        assert rejected is None
    assert accuracy_passed(readings) is bool(accuracy)
