"""Validation tests for shared structured agent-response schemas."""

import pytest
from pydantic import ValidationError

from server.api.protocol import PerformanceRound
from vibesys.evaluators.perf_reply import (
    LatencyStats,
    LoadLevelMetrics,
    ThroughputStats,
)
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import ImplementerResponse
from vibesys.roles.judge import JudgeResponse
from vibesys.roles.profiler import ProfilerSummary
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.schemas import (
    HYPOTHESIS_TITLE_MAX_LEN,
    SkillResourceSelection,
    derive_hypothesis_title,
    normalize_hypothesis_title,
)
from vibesys.search.hypothesis import OrchestratorPlan


def _profiler_summary(
    *,
    perf_metric: float | None = None,
    metrics: dict[str, float] | None = None,
) -> ProfilerSummary:
    return ProfilerSummary(
        analysis="analysis",
        bottlenecks="bottlenecks",
        suggestions="suggestions",
        perf_metric=perf_metric,
        metrics=metrics or {},
    )


def test_skill_resource_selection_forbids_unknown_fields():  # noqa: ANN201  # tracked: #288
    unknown_field = {"unexpected": True}
    with pytest.raises(ValidationError, match="unexpected"):
        SkillResourceSelection(
            skill="portable",
            resource_paths=[],
            purpose="Useful for this task.",
            **unknown_field,
        )


@pytest.mark.parametrize("field", ["skill", "purpose"])
def test_skill_resource_selection_rejects_whitespace_required_fields(field):  # noqa: ANN001, ANN201  # tracked: #288
    values = {"skill": "portable", "purpose": "Useful for this task."}
    values[field] = "   "

    with pytest.raises(ValidationError, match="non-whitespace"):
        SkillResourceSelection(**values)


def test_agent_skill_selection_fields_are_zero_to_many_by_default():  # noqa: ANN201  # tracked: #288
    plan = OrchestratorPlan(task="work", pass_criteria="passes", reasoning="reason")  # noqa: S106  # tracked: #288
    implementer = ImplementerResponse(summary="done", expected_behavior="works")
    judge = JudgeResponse(analysis="clean", feedback="", verdict=Verdict.PASS)
    single_agent = SingleAgentRoundResponse(
        summary="done",
        expected_behavior="works",
        self_review="clean",
        feedback="",
        verdict=Verdict.PASS,
        bottlenecks="none",
        suggestions="none",
        profile_analysis="none",
    )

    assert plan.recommended_skills == []
    assert implementer.skill_context_updates == []
    assert judge.skills_used == []
    assert single_agent.skill_context_updates == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_profiler_summary_rejects_non_finite_perf_metric(value):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(ValidationError, match="finite number"):
        _profiler_summary(perf_metric=value)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_profiler_summary_rejects_non_finite_multi_objective_metric(value):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(ValidationError, match="finite number"):
        _profiler_summary(metrics={"throughput": value})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_single_agent_response_rejects_non_finite_perf_metric(value):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(ValidationError, match="finite number"):
        SingleAgentRoundResponse(
            summary="summary",
            expected_behavior="expected behavior",
            self_review="self review",
            feedback="",
            verdict=Verdict.PASS,
            bottlenecks="bottlenecks",
            suggestions="suggestions",
            profile_analysis="analysis",
            perf_metric=value,
            perf_unit="req/s",
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_performance_stats_reject_non_finite_values(value):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(ValidationError, match="finite number"):
        LatencyStats(
            mean_ms=value,
            p50_ms=1.0,
            p90_ms=1.0,
            p95_ms=1.0,
            p99_ms=1.0,
        )

    with pytest.raises(ValidationError, match="finite number"):
        ThroughputStats(request_throughput=value, token_throughput=1.0)

    with pytest.raises(ValidationError, match="finite number"):
        LoadLevelMetrics(
            target_rate=value,
            actual_rate=1.0,
            num_requests=1,
            num_completed=1,
            num_failed=0,
            duration=1.0,
            throughput=ThroughputStats(request_throughput=1.0, token_throughput=1.0),
        )

    with pytest.raises(ValidationError, match="finite number"):
        PerformanceRound(
            round=1,
            perf_metric=value,
            perf_unit="req/s",
            passed=True,
        )


def test_orchestrator_plan_title_defaults_to_empty_string():  # noqa: ANN201  # tracked: #288
    plan = OrchestratorPlan(task="work", pass_criteria="passes", reasoning="reason")  # noqa: S106  # tracked: #288

    assert plan.title == ""


def test_orchestrator_plan_loads_old_shaped_data_without_a_title_key():  # noqa: ANN201  # tracked: #288
    """A state file persisted before the title field existed has no ``title`` key."""
    legacy_payload = {
        "hypothesis_id": "legacy",
        "hypothesis": "old claim",
        "task": "work",
        "pass_criteria": "passes",
        "reasoning": "reason",
    }

    plan = OrchestratorPlan.model_validate(legacy_payload)

    assert plan.title == ""


@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
def test_normalize_hypothesis_title_passes_through_empty_input(raw):  # noqa: ANN001, ANN201  # tracked: #288
    assert normalize_hypothesis_title(raw) == ""


def test_normalize_hypothesis_title_strips_and_collapses_whitespace():  # noqa: ANN201  # tracked: #288
    assert normalize_hypothesis_title("  Batch   the\n\tdecode   path  ") == "Batch the decode path"


def test_normalize_hypothesis_title_truncates_on_a_word_boundary_with_ellipsis():  # noqa: ANN201  # tracked: #288
    title = "A" * 50 + " " + "B" * 20

    result = normalize_hypothesis_title(title)

    assert result == "A" * 50 + "…"
    assert len(result) <= HYPOTHESIS_TITLE_MAX_LEN


def test_normalize_hypothesis_title_truncates_without_a_boundary():  # noqa: ANN201  # tracked: #288
    title = "A" * 65

    result = normalize_hypothesis_title(title)

    assert result == "A" * 59 + "…"
    assert len(result) == HYPOTHESIS_TITLE_MAX_LEN


def test_normalize_hypothesis_title_leaves_a_title_within_budget_untouched():  # noqa: ANN201  # tracked: #288
    title = "Batch decode requests by KV-cache page"

    assert normalize_hypothesis_title(title) == title


@pytest.mark.parametrize("claim", ["", "   ", "\n\t "])
def test_derive_hypothesis_title_returns_none_for_empty_claim(claim):  # noqa: ANN001, ANN201  # tracked: #288
    assert derive_hypothesis_title(claim) is None


def test_derive_hypothesis_title_takes_the_first_sentence():  # noqa: ANN201  # tracked: #288
    claim = "Batching decode requests reduces overhead. It also raises latency variance."

    assert derive_hypothesis_title(claim) == "Batching decode requests reduces overhead"


def test_derive_hypothesis_title_takes_the_first_line_when_there_is_no_sentence_break():  # noqa: ANN201  # tracked: #288
    claim = "Batching decode requests reduces overhead\nfurther detail on the mechanism"

    assert derive_hypothesis_title(claim) == "Batching decode requests reduces overhead"


def test_derive_hypothesis_title_truncates_long_claims():  # noqa: ANN201  # tracked: #288
    claim = ("A" * 50 + " " + "B" * 20) + "."

    result = derive_hypothesis_title(claim)

    assert result == "A" * 50 + "…"
    assert result is not None
    assert len(result) <= HYPOTHESIS_TITLE_MAX_LEN
