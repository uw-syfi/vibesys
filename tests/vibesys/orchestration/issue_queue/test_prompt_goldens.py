"""Byte-exact goldens for what the issue queue sends to its agents.

Each prompt is rendered the way the render operation renders it: the template named by its
typed context, with the context's fields and the plan's run-level variables. Regenerate with
``UPDATE_PROMPT_SNAPSHOTS=1`` and review every fixture diff as a prompt diff.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.vibesys.golden.helpers import assert_exact_text

from vibesys.issue_queue_board import IssueQueueTexts
from vibesys.issue_queue_core import CORE_ROLES
from vibesys.orchestration.issue_queue.core_policy.api import prompts
from vibesys.orchestration.issue_queue.strategy.api import (
    BOOTSTRAP_TEMPLATE,
    ImplementPrompt,
    IssueBrief,
    IssueDetail,
    PerformancePrompt,
    PriorPerformance,
    PromptContext,
    ReplyCorrectionPrompt,
    ResumePrompt,
    ReviewPrompt,
    WaitUnrecordedPrompt,
)
from vs_core.api import EvidenceId, RevisionId, RevisionRef
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentRole, ProfileExecution, RunFacts

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "prompts"

_CONFIGURED = RunFacts(
    domain_id="llm-serving",
    objective="Maximize measured token throughput without losing correctness.",
    reference_location="reference/model.py",
    accuracy_command="uv run check-accuracy",
    benchmark_command="uv run benchmark",
    profiler_id="torch",
    profile_execution=ProfileExecution.LOCAL,
    environment_notes="Use CUDA with bfloat16.",
)
_BARE = RunFacts(domain_id="generic", objective="Build the candidate.")

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
_REVISION = RevisionRef(revision_id=RevisionId(root="a1b2c3"), digest="git-commit:a1b2c3")
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
    "resume": (
        ResumePrompt(
            role="implementer",
            retained_revision=_REVISION,
            timed_out=False,
            evidence=(EvidenceId(root="e1"),),
            repeated_failure="same error three times",
        ),
        _CONFIGURED,
    ),
    "resume_timed_out": (
        ResumePrompt(role="judge", retained_revision=None, timed_out=True),
        _CONFIGURED,
    ),
    "reply_correction": (
        ReplyCorrectionPrompt(role="judge", error="verdict: Input should be 'pass' or 'fail'"),
        _CONFIGURED,
    ),
    "wait_unrecorded": (WaitUnrecordedPrompt(), _CONFIGURED),
    "bootstrap_configured": (BOOTSTRAP_TEMPLATE, _CONFIGURED),
    "bootstrap_bare": (BOOTSTRAP_TEMPLATE, _BARE),
}


def _render(context: PromptContext | str, facts: RunFacts) -> str:
    variables: dict[str, object] = {
        "facts": facts,
        "objective": facts.objective,
        "agent_evaluation": True,
    }
    if isinstance(context, str):
        return IssueQueueTexts(variables).issue_body(context)
    fields = {name: getattr(context, name) for name in type(context).model_fields}
    template = str(fields.pop("template"))
    return TemplateRenderer(Path(prompts.__file__).parent).render_template(
        f"{template}.j2", **variables, **fields
    )


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
