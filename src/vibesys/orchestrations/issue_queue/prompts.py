"""Prompt messages assembled from explicit policy inputs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vibesys.orchestrations.issue_queue.models import IssueQueueOptions, IssueQueueState
    from vs_issue_board.api import Issue
    from vs_runtime.api import RunFacts


def _commands(facts: RunFacts) -> str:
    return (
        f"Accuracy command: {facts.accuracy_command or '<not configured>'}\n"
        f"Benchmark command: {facts.benchmark_command or '<not configured>'}"
    )


def bootstrap_description(facts: RunFacts) -> str:
    """Describe the first candidate-facing implementation issue."""
    return f"""## Background

Build a production-ready inference server for the reference implementation at
`{facts.reference_location}`. Inspect the reference and checker rather than
substituting a generic model wrapper. Keep all candidate work in this workspace.

Objective: {facts.objective or '<not specified>'}

{facts.environment_notes}

## Acceptance criteria

- Implement the model and its weight loading from the provided local inputs.
- Expose `/v1/completions` streaming non-empty token deltas and `/health`.
- Preserve the checker-required `VibeServeModel.from_pretrained` and `generate` interface.
- Add pytest coverage for health, completion streaming, accuracy, and a benchmark smoke run.
- Run the configured checks and leave a reproducible candidate.

## Commands

{_commands(facts)}
"""


def implementer_message(issue: Issue, facts: RunFacts, prior_review: dict[str, object] | None) -> str:
    """Request one bounded implementation attempt."""
    review = ""
    if prior_review is not None:
        review = (
            "\nPrevious failed review:\n"
            f"Feedback: {prior_review.get('feedback', '')}\n"
            f"Analysis: {prior_review.get('analysis', '')}\n"
        )
    return f"""Work only on issue #{issue.id}: [{issue.type.value}] {issue.title}

{issue.description}

Reference: {facts.reference_location}
Objective: {facts.objective or '<not specified>'}
{_commands(facts)}
Runtime notes: {facts.environment_notes or '<none>'}
{review}
Read the current workspace, make the minimum sufficient change, run a focused
self-check, and return issue_id, summary, files_touched, and self_check.
"""


def judge_message(issue: Issue, facts: RunFacts) -> str:
    """Request an independent functional review."""
    return f"""Review issue #{issue.id}: [{issue.type.value}] {issue.title}

{issue.description}

Performance improvement is out of scope for this verdict. Inspect the candidate,
maintain relevant pytest tests, run them, and use the accuracy checker when one
is configured. A failing required correctness check requires a fail verdict.
{_commands(facts)}

For an unrelated bug, search the issue board before filing at most one bug.
Return issue_id, analysis, feedback, verdict, and new_issues_filed.
"""


def performance_message(
    *,
    iteration: int,
    facts: RunFacts,
    options: IssueQueueOptions,
    state: IssueQueueState,
) -> str:
    """Request one measured performance assessment and bounded queue update."""
    loads = (
        [level.model_dump(mode="json") for level in options.load_levels]
        if options.load_levels is not None
        else "discover a low, medium, high, and saturation workload"
    )
    prior = [record.model_dump(mode="json") for record in state.performance]
    return f"""Run performance evaluation {iteration}.

{_commands(facts)}
Runtime notes: {facts.environment_notes or '<none>'}
Objective: {facts.objective or '<not specified>'}
Configured load levels: {json.dumps(loads, sort_keys=True)}
Prior performance records: {json.dumps(prior, sort_keys=True)}

Inspect supported benchmark flags, measure across the requested loads, compare
with both the prior and best result, and identify the limiting resource. Search
the issue board before filing up to {options.max_issues_per_perf_eval} ranked,
evidence-backed bug, feature, or performance issues. Return the measured metrics,
analysis, evaluator_feedback, new_issue_ids, throughput_trend, and latency_trend.
"""


__all__ = [
    "bootstrap_description",
    "implementer_message",
    "judge_message",
    "performance_message",
]
