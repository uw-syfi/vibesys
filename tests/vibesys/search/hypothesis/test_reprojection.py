"""``HypothesisSearch``'s resume-time and lifecycle-guard behavior.

``HypothesisSearch.resume``/``finish`` are what
``loops/{multi,single}/session.py`` call every time a run starts or resumes
(``search.resume(previous, configured_metric_space)``) and, on the smaller
guard cases, ``validate_updates``/``delta_reason``. This file drives all of
that through the ``HypothesisSearch`` facade, not the underlying
``vibesys.search.hypothesis.transitions`` functions directly; only the
fixture helpers below (``start_hypothesis``/``append_round``, which build
test states rather than exercise behavior under test) still call into
``transitions`` for convenience.
"""

from __future__ import annotations

import pytest

from vibesys.evaluators.metrics import MetricComparison, MetricSpace, Objective
from vibesys.search.hypothesis import (
    HypothesisConfig,
    HypothesisResolution,
    HypothesisSearch,
    OrchestratorPlan,
)
from vibesys.search.hypothesis.plan import HypothesisStrategyUpdate
from vibesys.search.hypothesis.state import Hypothesis, HypothesisState
from vibesys.search.hypothesis.transitions import append_round, start_hypothesis
from vs_loop_state.api import PerfDeltaReason, RoundRecord

_NOISY_OPS = MetricSpace(
    objectives=(Objective(name="total_ops_per_sec", direction="max"),), relative_noise=0.05
)


def _search() -> HypothesisSearch:
    return HypothesisSearch(HypothesisConfig(max_rounds=3))


def _plan(identifier: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=identifier,
        hypothesis=f"claim {identifier}",
        task=f"implement {identifier}",
        pass_criteria="tests pass",  # noqa: S106
        reasoning="test the claim",
    )


def _round(  # noqa: PLR0913
    number: int,
    metric: float,
    *,
    hypothesis_id: str,
    parent_round: int | None = None,
    comparison: MetricComparison | None = None,
    outcome: str = "proven",
) -> RoundRecord:
    return RoundRecord(
        round_number=number,
        commit=f"{number:040x}",
        perf_metric=metric,
        perf_unit="total_ops_per_sec",
        passed=True,
        reviewed=True,
        hypothesis_id=hypothesis_id,
        hypothesis_declared_outcome="nominated",
        judge_verdict="pass",
        hypothesis_outcome=outcome,
        hypothesis_parent_round=parent_round,
        official_evaluation=True,
        perf_comparison=comparison,
    )


def _within_noise_run() -> HypothesisState:
    """Two official rounds one percent apart, in a five percent metric space."""
    state = HypothesisState(metrics=_NOISY_OPS)
    baseline = append_round(
        start_hypothesis(state, _plan("H-base"), started_round=1),
        _round(1, 100.0, hypothesis_id="H-base"),
        keep_active=False,
    )
    started = start_hypothesis(
        baseline, _plan("H-1"), started_round=2, parent_round=1, parent_commit=f"{1:040x}"
    )
    return append_round(
        started,
        _round(
            2,
            101.0,
            hypothesis_id="H-1",
            parent_round=1,
            comparison=MetricComparison.WITHIN_NOISE,
            outcome="inconclusive",
        ),
        keep_active=False,
    )


def _strip_stored_comparisons(state: HypothesisState) -> HypothesisState:
    """Return *state* as if written before comparisons were persisted."""
    payload = state.model_dump()
    for hypothesis in payload["hypotheses"]:
        for record in hypothesis["rounds"]:
            record["perf_comparison"] = None
    return HypothesisState.model_validate(payload)


def test_resume_reprojection_agrees_with_the_recorded_round_outcome() -> None:
    """Regression for #507: ``--resume`` must not contradict the round record."""
    completed = _within_noise_run()
    live = completed.by_id("H-1")

    reprojected = _search().resume(completed, _NOISY_OPS).by_id("H-1")

    assert live is not None
    assert reprojected is not None
    assert reprojected.resolution is live.resolution
    assert reprojected.resolution is HypothesisResolution.INCONCLUSIVE


def test_adopting_a_metric_space_rewrites_the_stored_space_and_evidence() -> None:
    """The run's launch configuration is written once; readers take it from state.

    Only a round with no stored comparison actually gets re-derived (see
    ``test_a_stored_comparison_survives_a_space_whose_tolerance_changed``
    below), so this uses a compatibility-path run predating that field.
    """
    strict_run = _strip_stored_comparisons(_within_noise_run()).model_copy(
        update={
            "metrics": MetricSpace(
                objectives=(Objective(name="total_ops_per_sec", direction="max"),)
            )
        },
        deep=True,
    )
    search = _search()
    strict_projected = search.resume(strict_run, strict_run.metrics).by_id("H-1")
    assert strict_projected is not None
    assert strict_projected.resolution is HypothesisResolution.PROVEN

    adopted = search.resume(strict_run, _NOISY_OPS)
    hypothesis = adopted.by_id("H-1")

    assert adopted.metrics == _NOISY_OPS
    assert hypothesis is not None
    assert hypothesis.resolution is HypothesisResolution.INCONCLUSIVE


def test_a_stored_comparison_survives_a_space_whose_tolerance_changed() -> None:
    """The round answers for itself, so a re-configured space cannot rewrite it.

    Editing ``objectives.toml`` between resumes changes how later rounds are
    ordered, not how recorded ones were: a delta the run judged to be noise
    stays noise.
    """
    recorded = _within_noise_run()
    assert [record.perf_comparison for record in recorded.rounds] == [
        None,
        MetricComparison.WITHIN_NOISE,
    ]
    strict = MetricSpace(objectives=_NOISY_OPS.objectives)

    resolved = _search().resume(recorded, strict).by_id("H-1")

    assert resolved is not None
    assert resolved.resolution is HypothesisResolution.INCONCLUSIVE


def test_finish_hypothesis_clears_the_active_pointer_and_advances_the_revision() -> None:
    search = _search()
    started = start_hypothesis(HypothesisState(), _plan("H-1"), started_round=1)
    finished = search.finish(started)

    assert finished.active_hypothesis_id is None
    assert finished.experiment_revision == started.experiment_revision + 1
    assert search.finish(finished) == finished.clone()  # idempotent when nothing is active


def test_apply_strategy_updates_rejects_abandoning_the_active_hypothesis() -> None:
    """Guards a state shape ``HypothesisSearch.start`` cannot itself reach:

    ``start`` always rejects starting a new hypothesis while one is active
    before it would ever apply strategy updates, so this branch only
    protects a caller (or a future one) that applies updates without going
    through ``start`` first.
    """
    active = start_hypothesis(HypothesisState(), _plan("active"), started_round=1)
    update = HypothesisStrategyUpdate(
        hypothesis_id="active", disposition="abandoned", reason="stop"
    )

    with pytest.raises(ValueError, match="cannot abandoned active"):
        _search().validate_updates(active, [update])


def test_measurement_delta_reason_flags_a_self_reported_headline_with_no_measurement() -> None:
    """The one fallback ``measurement_delta_reason`` adds over reading
    ``hypothesis.measurement.delta_reason`` directly: an implementer-reported
    round carries no ``HypothesisMeasurement`` at all (see
    ``test_self_reported_improvement_never_resolves_proven`` in
    ``hypothesis_search/test_search.py``), so this walks the raw rounds
    instead. Used by the read model (``loops/hypothesis_readmodel.py``), not
    by anything inside ``search.hypothesis`` itself.
    """
    record = RoundRecord(
        round_number=1,
        commit="a" * 40,
        perf_metric=100.0,
        perf_unit="ops",
        passed=True,
        hypothesis_id="H-1",
        official_evaluation=True,
        perf_provenance="implementer",
    )
    hypothesis = Hypothesis(
        hypothesis_id="H-1", plan=_plan("H-1"), started_round=1, rounds=[record]
    )

    assert hypothesis.measurement is None
    assert HypothesisSearch.delta_reason(hypothesis) is PerfDeltaReason.NOT_FRAMEWORK_MEASURED


def test_measurement_delta_reason_is_none_with_no_evidence_either_way() -> None:
    hypothesis = Hypothesis(hypothesis_id="H-1", plan=_plan("H-1"), started_round=1)
    assert HypothesisSearch.delta_reason(hypothesis) is None
