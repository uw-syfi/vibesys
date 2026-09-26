"""The progress board's framework log: plan, verdict, gates, exhaustion.

This is job (2) of the progress board (see
:mod:`vibesys.orchestration.memory` and :mod:`vibesys.orchestration.artifacts`
for the other two jobs): the framework's own narration of what it decided and
observed each round, distinct from role handoffs (job 1, ``artifacts.py``)
and declared agent memory (job 3, ``memory.py``).

Every ``render_*`` function here is pure: it takes the same typed data a role
turn or gate already produced and returns the exact Markdown block the board
used to write immediately. :func:`write` is the one place that turns a
rendered block into a file mutation, reusing the replace-by-heading merge the
board has always used so a resumed round replaces its own stable heading
instead of duplicating it.

This module lives under ``vibesys.orchestration`` (its own logic, not the
dissolved ``agent_run``) so the host itself -- ``orchestration.state``'s
``ctx.state.commit`` and
``orchestration.gates``'s ``ctx.gates.run`` -- can call :func:`write`
directly. Framework-log entries only need to exist before the next agent
turn reads them; which workspace snapshot happens to record their git diff
does not matter. Gate outcomes are written synchronously by ``ctx.gates.run``
itself (matching the snapshot ``ctx.gates.run`` already takes right after).
Every other entry is buffered by the strategy (a plain ``list[str]``, not
persisted state) and flushed by ``ctx.state.commit`` -- the host -- right
after it durably commits typed state, which is always before the next turn.
Resume never depends on the board file: durable state alone decides what
happens next, and a crash before a pending block is flushed just means that
block is re-derived (recomputed by the same turn/decision) rather than
replayed from disk.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.evaluators.perf_reply import ProfilerSummary
    from vibesys.evaluators.validation_recipe import FrameworkValidationResult
    from vibesys.roles.implementer import ImplementerResponse
    from vibesys.roles.judge import JudgeResponse
    from vibesys.roles.pre_round import PreRoundDecision
    from vibesys.roles.single_agent import SingleAgentRoundResponse
    from vibesys.search.hypothesis import OrchestratorPlan

#: Every block this module renders starts with this heading shape; the round
#: number is recovered from it rather than threaded separately through the
#: buffer, so the buffer stays a plain ``list[str]``.
_HEADING_ROUND = re.compile(r"^## Round (\d+) — ")

_PROGRESS_HEADER = "# Progress\n\n"


def render_pre_round_decision(round_number: int, decision: PreRoundDecision) -> str:
    """Render the orchestrator's pre-round decision."""
    return (
        f"## Round {round_number} — Orchestrator (pre-round)\n"
        f"- **need_profile**: {decision.need_profile}\n"
        f"- **profile_focus**: {decision.profile_focus}\n"
        f"- **reasoning**: {decision.reasoning}\n"
    )


def render_profiler_summary(round_number: int, summary: ProfilerSummary) -> str:
    """Render a profiler summary for one round."""
    perf_line = ""
    if summary.perf_metric is not None:
        unit = summary.perf_unit or ""
        perf_line = f"- **perf_metric**: {summary.perf_metric} {unit}\n".rstrip() + "\n"
    return (
        f"## Round {round_number} — Profiler\n"
        f"{perf_line}"
        f"### Bottlenecks\n{summary.bottlenecks}\n\n"
        f"### Suggestions\n{summary.suggestions}\n\n"
        f"### Analysis\n{summary.analysis}\n"
    )


def render_orchestrator_plan(round_number: int, plan: OrchestratorPlan) -> str:
    """Render the orchestrator plan and any requested round rollback."""
    revert_line = ""
    if plan.revert_to_round is not None:
        revert_line = f"- **revert_to_round**: {plan.revert_to_round}\n"
    strategy_lines = "".join(
        f"- **hypothesis_update**: {update.hypothesis_id} -> "
        f"{update.disposition}: {update.reason}\n"
        for update in plan.hypothesis_updates
    )
    return (
        f"## Round {round_number} — Orchestrator (plan)\n"
        f"{revert_line}"
        f"{strategy_lines}"
        f"- **hypothesis_id**: {plan.hypothesis_id or '(unspecified)'}\n"
        f"- **reasoning**: {plan.reasoning}\n\n"
        f"### Hypothesis\n{plan.hypothesis or '(unspecified)'}\n\n"
        f"### Activation evidence\n{plan.activation_evidence or '(unspecified)'}\n\n"
        f"### Falsification criteria\n{plan.falsification_criteria or '(unspecified)'}\n\n"
        f"### Expected effect (forecast)\n{plan.expected_effect or '(unspecified)'}\n\n"
        "### Minimum acceptance criteria\n"
        f"{plan.minimum_acceptance_criteria or '(unspecified)'}\n\n"
        f"### Invariants\n{plan.invariants or '(unspecified)'}\n\n"
        f"### Task\n{plan.task}\n\n"
        f"### Pass criteria\n{plan.pass_criteria}\n"
    )


def render_hypothesis_continuation(
    round_number: int,
    *,
    plan: OrchestratorPlan,
    started_round: int,
    continuation_step: str,
) -> str:
    """Render the continuation plan used to refine the active hypothesis."""
    return (
        f"## Round {round_number} — Active hypothesis continuation\n"
        f"- **hypothesis_id**: {plan.hypothesis_id}\n"
        f"- **started_round**: {started_round}\n"
        "- **designer_invocation**: skipped; implementer retains ownership\n\n"
        f"### Hypothesis\n{plan.hypothesis or '(unspecified)'}\n\n"
        f"### Current continuation delta\n{continuation_step}\n"
    )


def render_implementer(round_number: int, retry: int, response: ImplementerResponse) -> str:
    """Render one implementer attempt and its reported result."""
    perf_line = ""
    if response.perf_metric is not None:
        unit = response.perf_unit or ""
        perf_line = (
            f"- **perf_metric**: {response.perf_metric} {unit}\n".rstrip()
            + "\n"
            + f"- **metrics**: {response.metrics}\n"
            + f"- **evaluation_artifact**: {response.evaluation_artifact or '(missing)'}\n"
        )
    candidate_line = (
        f"- **candidate_disposition**: {response.candidate_disposition.value}\n"
        f"- **candidate_metrics**: {response.candidate_metrics or {}}\n"
        "- **candidate_evaluation_artifact**: "
        f"{response.candidate_evaluation_artifact or '(missing)'}\n"
        f"- **candidate_operating_point**: {response.candidate_operating_point or '(none)'}\n"
        "- **candidate_retention_reason**: "
        f"{response.candidate_retention_reason or '(none)'}\n"
    )
    return (
        f"## Round {round_number} — Implementer (attempt {retry})\n"
        f"- **expected_behavior**: {response.expected_behavior}\n"
        f"- **hypothesis_outcome**: {response.hypothesis_outcome.value}\n"
        f"- **next_step**: {response.next_step or '(none)'}\n\n"
        f"{perf_line}"
        f"{candidate_line}\n"
        f"### Summary\n{response.summary}\n\n"
        f"### Evidence\n{response.evidence or '(none)'}\n"
    )


def render_judge(round_number: int, retry: int, response: JudgeResponse) -> str:
    """Render one judge response."""
    return (
        f"## Round {round_number} — Judge (attempt {retry})\n"
        f"- **verdict**: {response.verdict.value}\n\n"
        f"### Analysis\n{response.analysis}\n\n"
        f"### Feedback\n{response.feedback}\n"
    )


def render_judge_skipped(
    round_number: int,
    *,
    outcome: str,
    judge_every: int,
) -> str:
    """Render the reason the judge step was skipped."""
    return (
        f"## Round {round_number} — Independent review deferred\n"
        f"- **implementer_outcome**: {outcome}\n"
        f"- **policy**: review every {judge_every} rounds, on nomination, and on the final round\n"
        "- **official_gates**: not run; all evidence this round is provisional\n"
    )


def render_official_evaluation_decision(  # noqa: PLR0913  # LW-011115 [PLR0913]; This formatter writes round/attempt plus the already-computed run/reason and cadence counts; a new decision object would only repack values for one Markdown block.
    round_number: int,
    retry: int,
    *,
    run: bool,
    reason: str,
    official_eval_every: int,
    provisional_candidates: int,
) -> str:
    """Render the framework's official evaluation decision."""
    decision = "run" if run else "deferred"
    return (
        f"## Round {round_number} — Official evaluation policy (attempt {retry})\n"
        f"- **decision**: {decision}\n"
        f"- **reason**: {reason}\n"
        f"- **cadence**: every {official_eval_every} accepted candidate checkpoints\n"
        f"- **provisional_candidates_before_this_round**: {provisional_candidates}\n"
    )


def render_single_agent_round(
    round_number: int,
    retry: int,
    response: SingleAgentRoundResponse,
) -> str:
    """Render the result of a single-agent round."""
    perf_line = ""
    if response.perf_metric is not None:
        unit = response.perf_unit or ""
        perf_line = f"- **perf_metric**: {response.perf_metric} {unit}\n".rstrip() + "\n"
    candidate_line = (
        f"- **candidate_disposition**: {response.candidate_disposition.value}\n"
        f"- **candidate_metrics**: {response.candidate_metrics or {}}\n"
        "- **candidate_evaluation_artifact**: "
        f"{response.candidate_evaluation_artifact or '(missing)'}\n"
        f"- **candidate_operating_point**: {response.candidate_operating_point or '(none)'}\n"
        "- **candidate_retention_reason**: "
        f"{response.candidate_retention_reason or '(none)'}\n"
    )
    return (
        f"## Round {round_number} — Single-agent (attempt {retry})\n"
        f"- **verdict**: {response.verdict.value}\n"
        f"- **expected_behavior**: {response.expected_behavior}\n"
        f"{perf_line}"
        f"{candidate_line}"
        f"### Summary\n{response.summary}\n\n"
        f"### Self-review\n{response.self_review}\n\n"
        f"### Feedback\n{response.feedback}\n\n"
        f"### Bottlenecks\n{response.bottlenecks}\n\n"
        f"### Suggestions\n{response.suggestions}\n\n"
        f"### Profile analysis\n{response.profile_analysis}\n"
    )


def render_framework_accuracy_gate(
    round_number: int,
    retry: int,
    *,
    command: str,
    passed: bool,
    output: str,
) -> str:
    """Render the framework accuracy-gate result."""
    verdict = "pass" if passed else "fail"
    return (
        f"## Round {round_number} — Framework accuracy gate (attempt {retry})\n"
        f"- **verdict**: {verdict}\n"
        f"- **command**: `{command}`\n\n"
        f"### Output\n{output or '(no output)'}\n"
    )


def render_framework_validation_gate(
    round_number: int,
    retry: int,
    *,
    artifact: str,
    results: list[FrameworkValidationResult],
) -> str:
    """Render the deterministic local validation gate for the progress ledger."""
    passed = bool(results) and all(result.passed for result in results)
    lines = [
        f"## Round {round_number} — Framework local validation (attempt {retry})",
        f"- **verdict**: {'pass' if passed else 'fail'}",
        f"- **artifact**: `{artifact}`",
    ]
    for result in results:
        source = "reused" if result.reused else "executed"
        lines.append(
            f"- **{result.recipe.name}**: {'pass' if result.passed else 'fail'} "
            f"({source}, inputs `{result.input_digest[:12]}`)"
        )
    return "\n".join(lines) + "\n"


def render_framework_benchmark(  # noqa: PLR0913  # LW-040106 [PLR0913]; the parameters are independent injected collaborators or options, and bundling them would hide ownership.
    round_number: int,
    retry: int,
    *,
    command: str,
    passed: bool,
    metric_name: str | None,
    metric_value: float | None,
    output: str,
) -> str:
    """Render framework benchmark metrics and diagnostics."""
    verdict = "pass" if passed else "fail"
    metric_line = (
        f"- **{metric_name}**: {metric_value}\n"
        if metric_name is not None and metric_value is not None
        else ""
    )
    return (
        f"## Round {round_number} — Framework benchmark (attempt {retry})\n"
        f"- **verdict**: {verdict}\n"
        f"- **command**: `{command}`\n"
        f"{metric_line}\n"
        f"### Output\n{output or '(no output)'}\n"
    )


def render_exhaustion_note(round_number: int, attempts: int, last_feedback: str) -> str:
    """Render a note that judge retries were exhausted."""
    return (
        f"## Round {round_number} — Judge loop exhausted\n"
        f"- **attempts**: {attempts}\n"
        f"- **last_feedback**: {last_feedback}\n"
    )


def _round_number(block: str) -> int:
    heading = block.splitlines()[0]
    match = _HEADING_ROUND.match(heading)
    if match is None:
        message = f"framework log block missing a round heading: {heading!r}"
        raise ValueError(message)
    return int(match.group(1))


def write(progress_path: Path, block: str) -> None:
    """Write one framework-owned progress section idempotently.

    A run can be resumed after a process exits between recording a phase
    result and finishing the round. The resumed phase has the same stable
    Markdown heading (round, role, and attempt), so replace that section
    instead of appending a duplicate. Distinct attempts retain distinct
    headings and therefore remain separate audit entries.

    Self-contained (does not import ``vibesys.orchestration.memory``, which
    itself would depend on this module for its own writes): ensures the
    progress document exists the same way
    ``memory.ensure_progress_file`` does, in the ``.md`` or directory
    layout the caller already resolved.
    """
    round_number = _round_number(block)
    if progress_path.suffix == ".md":
        if not progress_path.exists():
            progress_path.parent.mkdir(parents=True, exist_ok=True)
            progress_path.write_text(_PROGRESS_HEADER)
    else:
        progress_path.mkdir(parents=True, exist_ok=True)
    document = (
        progress_path
        if progress_path.suffix == ".md"
        else progress_path / f"round-{round_number:04d}.md"
    )
    if not document.exists() and document != progress_path:
        document.write_text(f"# Round {round_number}\n\n")

    heading = block.splitlines()[0]
    normalized_block = block.rstrip("\n") + "\n\n"
    lines = document.read_text(encoding="utf-8").splitlines(keepends=True)
    output: list[str] = []
    replaced = False
    index = 0
    while index < len(lines):
        if lines[index].rstrip("\r\n") != heading:
            output.append(lines[index])
            index += 1
            continue

        if not replaced:
            output.append(normalized_block)
            replaced = True
        index += 1
        # A framework section owns its H3 children, but not a neighboring H2.
        # Operators and recovery tooling may append evidence under their own H2
        # between an interrupted phase and resume. Preserve that evidence when
        # replacing the stable framework heading.
        while index < len(lines) and not lines[index].startswith("## "):
            index += 1

    if not replaced:
        with document.open("a", encoding="utf-8") as fh:
            fh.write(normalized_block)
        return

    # Replacement rewrites an existing audit section. Keep the prior file
    # intact if the process exits during the write, then atomically publish
    # the completed document.
    replacement = document.with_name(f".{document.name}.tmp")
    replacement.write_text("".join(output), encoding="utf-8")
    replacement.replace(document)
