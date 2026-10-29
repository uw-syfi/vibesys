"""Byte-exact goldens for what the issue queue sends to its agents.

Each prompt is rendered the way the render operation renders it: the template named by its
typed context, with the context's fields and the plan's run-level variables. Regenerate with
``UPDATE_PROMPT_SNAPSHOTS=1`` and review every fixture diff as a prompt diff.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.golden.helpers import assert_exact_text
from tests.vibesys.reply_variables import context_variables

from vibesys.core_wiring import domain_prompt_sections
from vibesys.issue_queue_board import IssueQueueTexts
from vibesys.issue_queue_core import CORE_ROLES
from vibesys.metrics import MetricRow
from vibesys.orchestration.issue_queue.core_policy.api import prompts
from vibesys.orchestration.issue_queue.models import IssuePerfEvalResult
from vibesys.orchestration.issue_queue.search.api import (
    BOOTSTRAP_TEMPLATE,
    ImplementPrompt,
    IssueBrief,
    IssueDetail,
    PerformancePrompt,
    PriorPerformance,
    PromptContext,
    ReviewEvaluation,
    ReviewPrompt,
    issue_queue,
)
from vibesys.prompts import CORE_PROTOCOL_DIR
from vs_core.api import EvidenceId, EvidenceKind, ObservationStatus, RevisionRef
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentRole, ProfileExecution, RunFacts
from vs_sim.api.testing import examples

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "prompts"

_CONFIGURED = RunFacts(
    agent_git_writable=True,
    domain_id="llm-serving",
    objective="Maximize measured token throughput without losing correctness.",
    reference_location="reference/model.py",
    accuracy_command="uv run check-accuracy",
    benchmark_command="uv run benchmark",
    profiler_id="torch",
    profile_execution=ProfileExecution.LOCAL,
    environment_notes="Use CUDA with bfloat16.",
)
_BARE = RunFacts(agent_git_writable=True, domain_id="generic", objective="Build the candidate.")

_ISSUE = IssueDetail(
    id=7,
    type="feature",
    status="in_progress",
    title="Implement streaming",
    attempts=1,
    description="## Acceptance criteria\n- stream non-empty token deltas",
)
_BOARD = (
    IssueBrief(id=7, type="feature", status="in_progress", title="Implement streaming", attempts=1),
    IssueBrief(id=8, type="bug", status="open", title="Health route 500s", attempts=0),
    IssueBrief(id=9, type="perf", status="closed", title="Batch prefill", attempts=2),
)
_PRIOR = (
    PriorPerformance(
        iteration=1,
        throughput_trend="improved",
        latency_trend="mixed",
        metrics_json='{"load_levels": []}',
        new_issues=2,
    ),
)
_LOADS = '[{"duration": 20, "max_tokens": 64, "rate": 4}]'

_CASES: dict[str, tuple[PromptContext | str, RunFacts]] = {
    "implement_first_attempt": (ImplementPrompt(issue=_ISSUE, board=_BOARD), _CONFIGURED),
    "implement_after_review": (
        ImplementPrompt(
            issue=_ISSUE,
            board=_BOARD,
            feedback="Add and test the health route.",
            analysis="The health route is missing.",
        ),
        _CONFIGURED,
    ),
    "implement_bare": (ImplementPrompt(issue=_ISSUE), _BARE),
    "review_configured": (
        ReviewPrompt(
            issue=_ISSUE,
            board=_BOARD,
            summary="Streams tokens.",
            files_touched=("server.py", "tests/test_server.py"),
            self_check="pytest passes",
        ),
        _CONFIGURED,
    ),
    "review_bare": (ReviewPrompt(issue=_ISSUE, summary="Done.", self_check=""), _BARE),
    "performance_discovered_loads": (
        PerformancePrompt(iteration=1, max_new_issues=3, board=_BOARD),
        _CONFIGURED,
    ),
    "performance_configured_loads_prior": (
        PerformancePrompt(
            iteration=2,
            max_new_issues=2,
            board=_BOARD,
            prior=_PRIOR,
            load_levels_json=_LOADS,
        ),
        _CONFIGURED,
    ),
    "bootstrap_configured": (BOOTSTRAP_TEMPLATE, _CONFIGURED),
    "bootstrap_bare": (BOOTSTRAP_TEMPLATE, _BARE),
}


def _render(context: PromptContext | str, facts: RunFacts) -> str:
    variables: dict[str, object] = {
        "facts": facts,
        "objective": facts.objective,
        "agent_evaluation": True,
        "domain_sections": domain_prompt_sections(facts),
    }
    if isinstance(context, str):
        return IssueQueueTexts(variables).issue_body(context)
    replies = issue_queue.reply_schemas
    return TemplateRenderer(
        Path(prompts.__file__).parent, fallback_roots=(CORE_PROTOCOL_DIR,)
    ).render_template(f"{context.template}.j2", **variables, **context_variables(context, replies))


@pytest.mark.parametrize("name", sorted(_CASES))
def test_issue_queue_prompt_matches_its_golden(name: str) -> None:
    context, facts = _CASES[name]
    assert_exact_text(_SNAPSHOT_DIR / f"{name}.txt", _render(context, facts))


@pytest.mark.parametrize("role", CORE_ROLES, ids=lambda role: role.id)
def test_issue_queue_system_prompt_matches_its_golden(role: AgentRole) -> None:
    assert_exact_text(_SNAPSHOT_DIR / f"system_{role.id}.txt", role.system_prompt)


_SERVING_ONLY_TERMS = (
    "VibeServeModel",
    "/v1/completions",
    "/health",
    "inference server",
    "inference-server",
)


@pytest.mark.parametrize("name", [n for n in sorted(_CASES) if n.startswith("bootstrap")])
def test_the_first_issue_text_names_no_kind_of_task_but_the_runs_own(name: str) -> None:
    context, facts = _CASES[name]
    text = _render(context, facts)
    assert facts.objective in text
    assert not [term for term in _SERVING_ONLY_TERMS if term in text]


@pytest.mark.parametrize("role", CORE_ROLES, ids=lambda role: role.id)
def test_no_issue_queue_system_prompt_assumes_a_serving_task(role: AgentRole) -> None:
    assert not [term for term in _SERVING_ONLY_TERMS if term in role.system_prompt]


def test_generic_run_prompts_do_not_invent_a_serving_contract() -> None:
    contexts = (
        ImplementPrompt(issue=_ISSUE),
        ReviewPrompt(issue=_ISSUE, summary="Done."),
        PerformancePrompt(iteration=2, max_new_issues=1, prior=_PRIOR),
    )
    rendered = [*(_render(context, _BARE) for context in contexts)]
    rendered.extend(role.system_prompt for role in CORE_ROLES)

    for prompt in rendered:
        for serving_assumption in (
            "inference server",
            "VibeServeModel",
            "/v1/completions",
            "/health",
            "model weights",
            "token throughput",
            "TTFT",
            "TPOT",
            "pytest",
            "server process",
        ):
            assert serving_assumption.casefold() not in prompt.casefold()


@pytest.mark.parametrize(
    ("facts", "expected", "foreign"),
    [
        (
            _CONFIGURED,
            ("throughput_trend", "latency_trend"),
            ("primary_metric_trend", "secondary_metric_trend"),
        ),
        (
            _BARE,
            ("primary_metric_trend", "secondary_metric_trend"),
            ("throughput_trend", "latency_trend"),
        ),
    ],
    ids=("llm-serving", "generic"),
)
def test_performance_prompt_field_names_match_the_domain_reply_schema(
    facts: RunFacts,
    expected: tuple[str, str],
    foreign: tuple[str, str],
) -> None:
    """The shared role prompt must not contradict either domain-selected reply schema."""
    task = _render(PerformancePrompt(iteration=1, max_new_issues=1), facts)
    system = next(role.system_prompt for role in CORE_ROLES if role.id == "plain-perf-evaluator")
    combined = f"{system}\n{task}"
    schema = json.dumps(IssuePerfEvalResult.model_json_schema())

    for field in expected:
        assert field in combined
        assert field in schema
    for field in foreign:
        assert field not in combined
    assert all(field in schema for field in (*expected, *foreign)), "one reply serves both"


def test_the_judge_is_shown_every_trusted_evaluation_it_is_given() -> None:
    """Regression: the review template read a variable the context never supplied."""
    evaluations = tuple(
        ReviewEvaluation(
            evidence_id=EvidenceId(root=f"evidence-{index}"),
            kind=EvidenceKind.BENCHMARK,
            revision=RevisionRef.of_git_commit(f"c{index}"),
            status=ObservationStatus.SUCCEEDED,
            passed=True,
        )
        for index in range(3)
    )
    text = _render(
        ReviewPrompt(issue=_ISSUE, summary="Done.", evaluations=evaluations), _CONFIGURED
    )

    for index in range(3):
        assert f"evidence-{index}" in text
    assert "Trusted evaluation results" in text
    assert "Trusted evaluation results" not in _render(
        ReviewPrompt(issue=_ISSUE, summary="Done."), _CONFIGURED
    )


@settings(max_examples=examples(pr=10, nightly=30), deadline=None, derandomize=True, database=None)
@given(
    value=st.floats(0.1, 1e6, allow_nan=False).map(lambda x: round(x, 1)),
    unit=st.sampled_from(["calls/s", "tokens/s", "ms", "req/min"]),
    direction=st.sampled_from(["max", "min"]),
    passed=st.booleans(),
)
def test_a_review_citing_trusted_evidence_states_value_unit_direction_and_verdict(
    value: float, unit: str, direction: Literal["max", "min"], *, passed: bool
) -> None:
    evaluation = ReviewEvaluation(
        evidence_id=EvidenceId(root="evidence-0"),
        kind=EvidenceKind.BENCHMARK,
        revision=RevisionRef.of_git_commit("c0"),
        status=ObservationStatus.SUCCEEDED if passed else ObservationStatus.FAILED,
        passed=passed,
        metrics=(MetricRow(name="throughput", value=value, direction=direction, unit=unit),),
    )
    text = _render(
        ReviewPrompt(issue=_ISSUE, summary="Done.", evaluations=(evaluation,)), _CONFIGURED
    )

    assert f"{value} {unit}" in text
    assert f"better: {direction}" in text
    assert ("PASS" if passed else "FAIL") in text
