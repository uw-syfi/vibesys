"""Byte-exact goldens for the messages the issue queue sends to its agents.

Regenerate with ``UPDATE_PROMPT_SNAPSHOTS=1`` and review every fixture diff as
a prompt diff.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vibesys.orchestration.issue_queue import IssueQueueOptions, IssueQueueState
from vibesys.orchestration.issue_queue.agents import (
    IMPLEMENTER_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    PERFORMANCE_SYSTEM_PROMPT,
)
from vibesys.orchestration.issue_queue.prompts import (
    bootstrap_description,
    implementer_message,
    judge_message,
    performance_message,
)
from vs_issue_tracker.api import Issue, IssueType
from vs_runtime.api import ProfileExecution, RunFacts

_SNAPSHOT_DIR = Path(__file__).with_name("fixtures") / "prompts"

_CONFIGURED = RunFacts(
    domain_id="llm-serving",
    objective="Maximize measured token throughput without losing correctness.",
    reference_location="reference/model.py",
    accuracy_command="uv run check-accuracy",
    benchmark_command="uv run benchmark",
    profiler_id="torch",
    environment_notes="Use CUDA with bfloat16.",
)
_BARE = RunFacts(domain_id="generic", objective="Build the candidate.")
_REMOTE = _CONFIGURED.model_copy(update={"profile_execution": ProfileExecution.REMOTE})
_NO_PROFILER = _CONFIGURED.model_copy(update={"profiler_id": "none"})

_ISSUE = Issue(
    id=7,
    type=IssueType.FEATURE,
    title="Implement streaming",
    description="## Acceptance criteria\n- stream non-empty token deltas",
    created_by="test",
    created_iter=1,
    created_at="2026-01-01T00:00:00Z",
    updated_at="2026-01-01T00:00:00Z",
)
_REVIEW: dict[str, object] = {
    "feedback": "Add and test the health route.",
    "analysis": "The health route is missing.",
}
_LOADS = (
    {"rate": 1, "duration": 20, "max_tokens": 128},
    {"rate": 4, "duration": 20, "max_tokens": 64},
)
_PRIOR = IssueQueueState.model_validate(
    {
        "performance": (
            {
                "iteration": 1,
                "throughput_trend": "improved",
                "latency_trend": "mixed",
                "metrics": {"load_levels": [{"target_rate": 1.0}], "extra": {}},
                "new_issue_ids": (2, 3),
            },
        )
    }
)


def _options(loads: object) -> IssueQueueOptions:
    return IssueQueueOptions.model_validate(
        {
            "max_rounds": 2,
            "max_attempts_per_issue": 2,
            "max_issues_per_perf_eval": 3,
            "load_levels": loads,
        }
    )


def _case(name: str, text: str) -> tuple[str, str]:
    return name, text


def _rendered() -> list[tuple[str, str]]:
    empty = IssueQueueState()
    return [
        _case("system_implementer", IMPLEMENTER_SYSTEM_PROMPT),
        _case("system_judge", JUDGE_SYSTEM_PROMPT),
        _case("system_performance", PERFORMANCE_SYSTEM_PROMPT),
        _case("bootstrap_configured", bootstrap_description(_CONFIGURED)),
        _case("bootstrap_bare", bootstrap_description(_BARE)),
        _case("implementer_first_attempt", implementer_message(_ISSUE, _CONFIGURED, None)),
        _case("implementer_after_review", implementer_message(_ISSUE, _CONFIGURED, _REVIEW)),
        _case("implementer_after_empty_review", implementer_message(_ISSUE, _BARE, {})),
        _case("judge_configured", judge_message(_ISSUE, _CONFIGURED)),
        _case("judge_bare", judge_message(_ISSUE, _BARE)),
        _case(
            "performance_local_profiler_loads_prior",
            performance_message(
                iteration=2, facts=_CONFIGURED, options=_options(_LOADS), state=_PRIOR
            ),
        ),
        _case(
            "performance_remote_profiler",
            performance_message(iteration=1, facts=_REMOTE, options=_options(_LOADS), state=empty),
        ),
        _case(
            "performance_no_profiler_discovered_loads",
            performance_message(
                iteration=1, facts=_NO_PROFILER, options=_options(None), state=empty
            ),
        ),
        _case(
            "performance_bare_facts",
            performance_message(iteration=3, facts=_BARE, options=_options(None), state=_PRIOR),
        ),
    ]


@pytest.mark.parametrize(("name", "text"), _rendered(), ids=lambda value: str(value)[:40])
def test_issue_queue_message_matches_golden(name: str, text: str) -> None:
    snapshot = _SNAPSHOT_DIR / f"{name}.txt"
    if os.environ.get("UPDATE_PROMPT_SNAPSHOTS") == "1":
        snapshot.write_text(text, encoding="utf-8")
        return
    assert text == snapshot.read_text(encoding="utf-8")
