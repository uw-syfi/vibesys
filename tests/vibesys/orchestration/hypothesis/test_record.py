"""Round records separate agent claims from trusted gate measurements."""

from __future__ import annotations

from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from vibesys.orchestration.hypothesis import (
    HypothesisConfig,
    HypothesisSearch,
    InputBaseline,
    OrchestratorPlan,
)
from vibesys.orchestration.hypothesis.attempts import (
    AttemptState,
    JudgeReviewed,
    PerformanceProjection,
)
from vibesys.orchestration.hypothesis.record import RecordInput, build_round_record
from vibesys.orchestration.hypothesis.state import Hypothesis, HypothesisState
from vibesys.orchestration.metrics import FrameworkBenchmarkOutcome, MetricSpace, Objective
from vibesys.orchestration.multi.contracts import ImplementerResponse
from vs_loop_state.api import CandidateDisposition, HypothesisOutcome, RoundRecord


def _record_input() -> RecordInput:
    plan = OrchestratorPlan(
        hypothesis_id="cache",
        task="cache responses",
        pass_criteria="behavior remains correct",  # noqa: S106  # LW-040062 [S106]; the argument is a fixture literal, not a credential.
        reasoning="reduce repeated work",
    )
    hypothesis = Hypothesis(
        hypothesis_id="cache",
        plan=plan,
        started_round=2,
        parent_round=1,
        parent_commit="parent-commit",
    )
    space = MetricSpace(objectives=(Objective("throughput", "max"),))
    state = HypothesisState(metrics=space, hypotheses=[hypothesis])
    parent = RoundRecord(
        round_number=1,
        commit="parent-commit",
        perf_metric=10.0,
        perf_unit="throughput",
        passed=True,
        metrics={"throughput": 10.0},
        official_evaluation=True,
        perf_provenance="framework",
    )
    attempt = AttemptState(
        agent_run_state=state,
        feedback=None,
        implementation=ImplementerResponse(
            summary="cache added",
            expected_behavior="higher throughput",
            hypothesis_outcome=HypothesisOutcome.SUPPORTED,
            candidate_disposition=CandidateDisposition.PARETO_FRONTIER,
            candidate_metrics={"throughput": 12.0},
            candidate_evaluation_artifact="profile.json",
        ),
        judge=JudgeReviewed("pass"),
        passed=True,
        official_reason="final_round",
        framework_benchmark=FrameworkBenchmarkOutcome(
            metric_name="throughput",
            metric_value=12.0,
            metric_direction="max",
            row={"throughput": 12.0},
        ),
    )
    projection = PerformanceProjection(
        metric=12.0,
        unit="throughput",
        provenance="framework",
        profile_skipped=False,
        accepted_metrics={"throughput": 11.0},
        accepted_evaluation_artifact="benchmark.json",
        next_single_response=None,
    )
    return RecordInput(
        state=state,
        records=[parent],
        round_number=2,
        hypothesis=hypothesis,
        plan=plan,
        attempt=attempt,
        projection=projection,
        reviewed=True,
        framework_benchmark_configured=True,
        accuracy_configured=True,
        candidate_commit="candidate-commit",
        backend_name="cli",
        driver_name="agentshim",
        provider="codex",
        model="model-a",
    )


def test_official_record_uses_benchmark_row_and_causal_parent() -> None:
    record = build_round_record(_record_input())

    assert record.official_evaluation
    assert record.metrics == {"throughput": 12.0}
    assert record.perf_baseline_round == 1
    assert record.perf_baseline_commit == "parent-commit"
    assert record.perf_baseline_metric == 10.0
    assert record.perf_delta_pct == 20.0
    assert record.candidate_retained
    assert record.implementer_driver == "agentshim"
    assert record.implementer_provider == "codex"
    assert record.implementer_model == "model-a"


def test_gate_retry_carries_approved_candidate_when_agent_omits_it() -> None:
    data = _record_input()
    data.hypothesis.gate_revalidation_pending = True
    data.hypothesis.gate_approved_candidate_disposition = CandidateDisposition.PARETO_FRONTIER.value
    data.hypothesis.gate_approved_candidate_metrics = {"throughput": 12.0}
    data.hypothesis.gate_approved_candidate_evaluation_artifact = "approved.json"
    assert data.attempt.implementation is not None
    data.attempt.implementation.candidate_disposition = CandidateDisposition.UNASSESSED
    data.attempt.passed = False

    record = build_round_record(replace(data, reviewed=False))
    assert not record.official_evaluation
    assert record.judge_verdict == "pass"
    assert record.candidate_disposition == CandidateDisposition.PARETO_FRONTIER.value
    assert record.candidate_metrics == {"throughput": 12.0}
    assert record.candidate_evaluation_artifact == "approved.json"
    assert record.candidate_retained


def test_failed_review_does_not_retain_agent_candidate() -> None:
    data = _record_input()
    data.attempt.passed = False
    data.attempt.judge = JudgeReviewed("fail")
    record = build_round_record(data)

    assert record.judge_verdict == "fail"
    assert not record.official_evaluation
    assert record.candidate_retained is False


# --- causal baseline selection (build_round_record -> metric_baseline) -----
#
# These drive the same fallback/fail-closed/renamed-unit/untrusted-provenance
# paths that ``metric_baseline`` implements, entirely through the record
# builder's public ``RecordInput``/``build_round_record`` surface: no
# ``vibesys.orchestration.hypothesis.transitions`` import needed.


def _official(number: int, metric: float, *, unit: str = "throughput", provenance="framework"):  # noqa: ANN001, ANN202  # LW-040063 [ANN001, ANN202]; this scripted double mirrors a production signature whose parameters are not annotated here. The helper is private to this test module and its return type is the local closure type.
    return RoundRecord(
        round_number=number,
        commit=f"parent-commit-{number}",
        perf_metric=metric,
        perf_unit=unit,
        passed=True,
        metrics={unit: metric},
        official_evaluation=True,
        perf_provenance=provenance,
    )


def test_baseline_falls_back_to_the_newest_official_reading_bounded_by_parent_round() -> None:
    data = _record_input()
    parent = _official(1, 10.0)
    later = _official(2, 20.0)
    data = replace(data, records=[parent, later])
    data.hypothesis.parent_round = 2
    data.hypothesis.parent_commit = "unmatched-commit"

    record = build_round_record(data)

    assert record.perf_baseline_round == 2
    assert record.perf_baseline_commit == later.commit
    assert record.perf_baseline_metric == 20.0


def test_baseline_fails_closed_with_an_unplaced_parent_commit_and_no_round_bound() -> None:
    data = _record_input()
    data = replace(data, records=[_official(1, 10.0)])
    data.hypothesis.parent_round = None
    data.hypothesis.parent_commit = "a commit no official round carries"

    record = build_round_record(data)

    assert record.perf_baseline_round is None
    assert record.perf_baseline_commit is None
    assert record.perf_baseline_metric is None
    assert record.perf_delta_pct is None


def test_baseline_matches_a_renamed_headline_unit_through_its_metrics_row() -> None:
    """A prior round whose *displayed* unit was renamed still matches, via its
    metrics row keyed by the current objective name ("throughput" here).
    """
    data = _record_input()
    renamed = RoundRecord(
        round_number=1,
        commit="renamed-commit",
        perf_metric=10.0,
        perf_unit="old_headline_name",
        passed=True,
        metrics={"throughput": 10.0},
        official_evaluation=True,
        perf_provenance="framework",
    )
    data = replace(data, records=[renamed])
    data.hypothesis.parent_round = 1
    data.hypothesis.parent_commit = renamed.commit

    record = build_round_record(data)

    assert record.perf_baseline_round == 1
    assert record.perf_baseline_metric == 10.0


def test_baseline_skips_an_agent_self_reported_prior_round() -> None:
    data = _record_input()
    self_reported = _official(1, 999.0, provenance="implementer")
    data = replace(data, records=[self_reported])
    data.hypothesis.parent_round = 1
    data.hypothesis.parent_commit = self_reported.commit

    record = build_round_record(data)

    assert record.perf_baseline_round is None
    assert record.perf_baseline_metric is None


# --- input baseline: the measured input tree roots every baseline chain ------

_INPUT = "input-commit"


def _with_input(data: RecordInput, value: float) -> RecordInput:
    baseline = InputBaseline(
        commit=_INPUT, metric="throughput", direction="max", metrics={"throughput": value}
    )
    return replace(data, state=data.state.model_copy(update={"input_baseline": baseline}))


def _measured(data: RecordInput, value: float) -> RecordInput:
    data.attempt.framework_benchmark = FrameworkBenchmarkOutcome(
        metric_name="throughput",
        metric_value=value,
        metric_direction="max",
        row={"throughput": value},
    )
    return replace(data, projection=replace(data.projection, metric=value, accepted_metrics={}))


def _from_input(data: RecordInput) -> RecordInput:
    data.hypothesis.parent_round = None
    data.hypothesis.parent_commit = _INPUT
    return replace(data, records=[])


def test_first_round_is_compared_with_the_input_baseline() -> None:
    data = _measured(_with_input(_from_input(_record_input()), 10.0), 12.0)

    record = build_round_record(data)

    assert record.perf_baseline_round is None
    assert record.perf_baseline_commit == _INPUT
    assert record.perf_baseline_metric == 10.0
    assert record.perf_delta_pct == 20.0
    assert record.hypothesis_outcome == "proven"
    assert record.candidate_retained is True


def test_round_slower_than_the_input_is_not_retained() -> None:
    """Regression: MPSC round 2 (98K, 8x below the input) became the anchor."""
    data = _measured(_with_input(_from_input(_record_input()), 800_000.0), 98_000.0)

    record = build_round_record(data)

    assert record.perf_baseline_metric == 800_000.0
    assert record.hypothesis_outcome == "disproven"
    assert record.candidate_retained is False


def test_round_built_on_a_non_retained_round_is_compared_with_the_input() -> None:
    """Regression: MPSC rounds 3 and 4 cleared ``proven`` against round 2.

    Round 2 was measured but not retained, so its reading stays on record
    without becoming the baseline for the round built on top of it.
    """
    regressed = _official(2, 98_000.0)
    regressed = replace(regressed, candidate_retained=False)
    data = _measured(_with_input(_record_input(), 800_000.0), 110_000.0)
    data = replace(data, records=[regressed], round_number=3)
    data.hypothesis.parent_round = 2
    data.hypothesis.parent_commit = regressed.commit

    record = build_round_record(data)

    assert record.perf_baseline_round is None
    assert record.perf_baseline_metric == 800_000.0
    assert record.perf_delta_pct is not None
    assert record.perf_delta_pct < 0
    assert record.hypothesis_outcome == "disproven"
    assert record.candidate_retained is False


@given(
    input_value=st.floats(min_value=1.0, max_value=1e6),
    readings=st.lists(st.floats(min_value=1.0, max_value=1e6), min_size=1, max_size=8),
    pareto=st.booleans(),
)
def test_no_round_is_anchored_below_the_best_retained_tree(
    input_value: float, readings: list[float], *, pareto: bool
) -> None:
    """Each round builds on the previous one, however it measured.

    Every baseline is the input or a retained round. A round is retained only
    if it beats the input and every earlier reading (a Pareto archive also
    keeps an exact tie). The final selection is the best reading, or nothing
    when the input still wins.
    """
    space = MetricSpace(objectives=(Objective("throughput", "max"),) if pareto else ())
    records: list[RoundRecord] = []
    for number, value in enumerate(readings, start=1):
        data = _measured(_with_input(_record_input(), input_value), value)
        data = replace(
            data,
            state=data.state.model_copy(update={"metrics": space}),
            records=list(records),
            round_number=number,
            candidate_commit=f"round-{number}",
        )
        data.hypothesis.parent_round = number - 1 or None
        data.hypothesis.parent_commit = records[-1].commit if records else _INPUT
        record = build_round_record(data)

        best_before = max([input_value, *(item.perf_metric or 0.0 for item in records)])
        if record.perf_baseline_round is None:
            assert record.perf_baseline_metric == input_value
        else:
            anchor = next(
                item for item in records if item.round_number == record.perf_baseline_round
            )
            assert anchor.candidate_retained is True
        beats = value >= best_before if pareto else value > best_before
        assert record.candidate_retained is beats
        records.append(record)

    search = HypothesisSearch(HypothesisConfig(max_rounds=len(readings)))
    winner = search.best(records, space=space)
    if max(readings) > input_value or (pareto and max(readings) == input_value):
        assert winner is not None
        assert winner.perf_metric == max(readings)
    else:
        assert winner is None


def test_reading_within_noise_of_the_input_is_not_retained() -> None:
    """An earlier non-retained reading near the input does not lower the bar."""
    space = MetricSpace(relative_noise=0.05)
    near_input = replace(_official(1, 790.0), candidate_retained=False)
    data = _measured(_with_input(_record_input(), 800.0), 835.0)
    data = replace(
        data,
        state=data.state.model_copy(update={"metrics": space}),
        records=[near_input],
        round_number=2,
    )
    data.hypothesis.parent_round = 1
    data.hypothesis.parent_commit = near_input.commit

    record = build_round_record(data)

    assert record.perf_baseline_metric == 800.0
    assert record.candidate_retained is False
