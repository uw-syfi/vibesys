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

Objective: {facts.objective or "<not specified>"}

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


def implementer_message(
    issue: Issue, facts: RunFacts, prior_review: dict[str, object] | None
) -> str:
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
Objective: {facts.objective or "<not specified>"}
{_commands(facts)}
Runtime notes: {facts.environment_notes or "<none>"}
{review}
Read `progress.md` for context, but do not edit it. Inspect the accuracy checker
and benchmark interface before changing their integration points. Make the
minimum sufficient change, then run the relevant pytest, accuracy, benchmark,
and streaming checks. Return exactly one JSON object matching:
{{"issue_id": {issue.id}, "summary": "...", "files_touched": ["..."],
 "self_check": "..."}}
"""


def judge_message(issue: Issue, facts: RunFacts) -> str:
    """Request an independent functional review."""
    return f"""Review issue #{issue.id}: [{issue.type.value}] {issue.title}

{issue.description}

Performance improvement is out of scope for this verdict. Inspect the candidate,
maintain relevant pytest tests, run them, and use the accuracy checker when one
is configured. A failing required correctness check requires a fail verdict.
{_commands(facts)}
Runtime notes: {facts.environment_notes or "<none>"}

Start the real service for an end-to-end `/health` and completion-stream smoke
test. When the benchmark is configured, run a short sanity workload and require
positive token throughput plus non-null TTFT and TPOT. For an unrelated bug,
list and search the issue board before filing at most one bug.

Return exactly one JSON object matching:
{{"issue_id": {issue.id}, "analysis": "...", "feedback": "...",
 "verdict": "pass" | "fail", "new_issues_filed": [1]}}
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
    profiler = (
        "No profiler is configured; do not attempt a profile."
        if facts.profiler_id == "none"
        else (
            f"Profiler `{facts.profiler_id}` is available through the fixed `profiler` "
            f"tool and executes on the {facts.profile_execution.value} path."
        )
    )
    return f"""Run performance evaluation {iteration}.

{_commands(facts)}
Runtime notes: {facts.environment_notes or "<none>"}
Objective: {facts.objective or "<not specified>"}
Reference: {facts.reference_location}
{profiler}
Configured load levels: {json.dumps(loads, sort_keys=True)}
Prior performance records: {json.dumps(prior, sort_keys=True)}

Use the configured loads as a starting ladder. If they do not expose saturation,
increase request rate until throughput plateaus, latency rises sharply, or
requests fail. Add one or two prompt-length or output-length workloads when the
shape could change the ceiling. Save and read structured benchmark output for
every run. Diagnose the limiting resource separately at low, medium, and
saturation load. Compare with both the immediately prior result and the best
recorded result.

Profiling is optional, limited to one run, and must answer a specific unresolved
bottleneck question. Do not attempt profiling when profiler is `none`. Always
stop any server process that this turn started, including after benchmark or
profiling failure.

Before filing, call list_issues for open work and search relevant keywords.
File at most {options.max_issues_per_perf_eval} non-duplicate issues. Rank them
by impact over effort. Each issue background must cite code_evidence,
metric_evidence, or profile_evidence and name expected_impact, effort, and scope.
Each description must contain `## Background`, `## Acceptance criteria`, and
`## Notes`.

Return exactly one JSON object with this schema:
{{
  "analysis": "trend comparison, saturation, resource diagnosis, and profiling decision",
  "metrics": {{
    "load_levels": [{{
      "target_rate": 1.0, "actual_rate": 1.0,
      "num_requests": 1, "num_completed": 1, "num_failed": 0,
      "duration": 1.0,
      "throughput": {{"request_throughput": 1.0, "token_throughput": 1.0}},
      "ttft": {{"mean_ms": 1.0, "p50_ms": 1.0, "p90_ms": 1.0,
                 "p95_ms": 1.0, "p99_ms": 1.0}},
      "tpot": null, "total_latency": null
    }}],
    "extra": {{}}
  }},
  "evaluator_feedback": ["measurement guidance for the next evaluator"],
  "new_issue_ids": [],
  "throughput_trend": "improved" | "regressed" | "mixed",
  "latency_trend": "improved" | "regressed" | "mixed"
}}
Populate every metric from actual output. Do not fabricate measurements.
"""


__all__ = [
    "bootstrap_description",
    "implementer_message",
    "judge_message",
    "performance_message",
]
