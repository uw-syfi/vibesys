"""Shared agent policy helpers for turns, prompts, and evidence."""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence  # noqa: TC003  # tracked: #288
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING

from vibesys.domains.base import DomainDefinition, DomainRole
from vibesys.domains.rendering import render_domain_section
from vibesys.events import (
    CoreEventType,
    EventStatus,
    FrameworkSource,
    JudgeResultData,
)
from vibesys.loops.agent import issue_board
from vibesys.loops.agent.hypotheses import (
    apply_strategy_updates,
    trusted_perf_provenance,
)
from vibesys.loops.agent.model import (
    AgentRunState,
    Hypothesis,
    HypothesisResolution,
)
from vibesys.loops.agent.roles import (
    SharedAgentHandle,
    _invoke_read_only_role,
)
from vibesys.loops.metrics import (
    Measurement,
    MetricSpace,
    Objective,
)
from vibesys.loops.profiler import mcp_spec as profiler_mcp_spec
from vibesys.profilers import (
    ProfilerKind,
    profiler_definition,
    require_profiler_kind,
)
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.render.sink import output_sink
from vibesys.schemas import (
    CandidateDisposition,
    FrameworkValidationResult,
    HypothesisOutcome,
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    ProfilerSummary,
    SingleAgentRoundResponse,
    SkillResourceSelection,
    ValidationRecipe,
    ValidationRecipeArtifact,
    Verdict,
    normalize_hypothesis_title,
)
from vibesys.skills import (
    ResolvedSkillSelection,
    build_skill_catalog,
    resolve_skill_selections,
)
from vs_agent.api import (
    AgentSessionKey,
    ResponseFallback,
    SessionScope,
)

if TYPE_CHECKING:
    from vibesys.loops.agent.hypothesis_controller import (
        ProfileGuidanceView,
    )
    from vibesys.run import LoopContext
    from vs_loop_state.api import RoundRecord

# Candidate process boundaries selected by ``--interface``. Language, tooling,
# and artifact requirements belong to the selected domain and input bundle.
_INTERFACES = ("inprocess", "service")
DEFAULT_INTERFACE = "inprocess"

_INNER_LOOPS = ("multi-agent", "single-agent")

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "agent"


def _backfill_revert_commit(
    state: Hypothesis | None,
    records: list[RoundRecord],
) -> bool:
    """Recover rollback provenance for state written before it was persisted.

    ``revert_applied`` was historically set only after the framework attempted
    the configured round checkout.  Older active state therefore identifies
    the parent round but not its commit.  Resolve that immutable commit from
    completed-round records once on resume so agent sandboxes do not need repository
    metadata to re-prove framework-owned setup.
    """
    if (
        state is None
        or not state.revert_applied
        or state.revert_commit is not None
        or state.parent_round is None
    ):
        return False
    parent = next(
        (record for record in records if record.round_number == state.parent_round),
        None,
    )
    if parent is None or parent.commit is None:
        return False
    state.revert_commit = parent.commit
    state.parent_commit = state.parent_commit or parent.commit
    return True


# `CONTINUE`/`SUPPORTED`/`NOMINATED` name an active or successful hypothesis;
# every other `HypothesisOutcome` member represents a failed one. Derive the
# failure set by subtraction so a new enum member defaults to "failed" rather
# than being silently omitted, as happened with `IMPLEMENTATION_FAILED`.
# `"rejected"` is a framework-only label (the reviewed-but-not-passed outcome
# assigned below) with no corresponding enum member, so it is added explicitly.
_FAILED_HYPOTHESIS_OUTCOMES = (
    frozenset(outcome.value for outcome in HypothesisOutcome)
    - {
        HypothesisOutcome.CONTINUE.value,
        HypothesisOutcome.SUPPORTED.value,
        HypothesisOutcome.NOMINATED.value,
    }
) | {"rejected"}
_MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW = 2
_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT = 8


def _implementation_requests_continuation(
    implementation: ImplementerResponse | None,
) -> bool:
    """Return whether an implementation response names unfinished scoped work."""
    if implementation is None:
        return False
    return bool(
        implementation.hypothesis_outcome
        in {
            HypothesisOutcome.CONTINUE,
            HypothesisOutcome.IMPLEMENTATION_FAILED,
            HypothesisOutcome.INCONCLUSIVE,
        }
        and implementation.next_step.strip()
    )


def _implementation_keeps_hypothesis_active(
    implementation: ImplementerResponse | None,
    *,
    continuation_rounds: int = 0,
) -> bool:
    """Return whether the same implementer goal owns the next round.

    A concrete continuation can keep the plan, workspace, and session through
    transient defects or a short multi-step implementation. Return control to
    the designer after two continuation rounds regardless of the outcome label,
    however, so ``continue`` cannot become an unbounded self-renewing lease.
    This is a design checkpoint, not a forced rollback: the designer may retain
    the same mechanism after comparing its remaining value with alternatives.
    An empty next step returns control to the designer as before.
    """
    return bool(
        _implementation_requests_continuation(implementation)
        and continuation_rounds < _MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW
    )


# ---------------------------------------------------------------------------
# Provisional Pareto checkpoint memory
# ---------------------------------------------------------------------------


def _record_candidate_metrics(record: RoundRecord) -> dict[str, float]:
    """Return the comparable objective row associated with *record*.

    Official metrics remain authoritative when present. Candidate metrics are
    a separate, explicitly provisional channel for representative evaluations
    that are useful for branch retention but must not update the canonical
    headline trajectory.
    """
    if record.official_evaluation and record.metrics:
        return record.metrics
    if _record_candidate_retained(record) is True:
        return record.candidate_metrics
    return {}


def _record_candidate_retained(record: RoundRecord) -> bool | None:
    """Read framework retention, with one isolated legacy-record adapter."""
    if record.candidate_retained is not None:
        return record.candidate_retained
    if record.judge_verdict is not None:
        # New records always carry the framework's typed verdict marker. For
        # them, explicit unknown retention must remain unknown.
        return None
    if record.candidate_disposition in {
        CandidateDisposition.PARETO_FRONTIER.value,
        CandidateDisposition.PREREQUISITE.value,
    }:
        return True
    if record.candidate_disposition == CandidateDisposition.DISCARD.value:
        return False
    if record.hypothesis_outcome == HypothesisResolution.PROVEN.value:
        return True
    return None


def _provisional_candidate_retained(
    disposition: CandidateDisposition,
) -> bool | None:
    """Translate an implementer disposition into provisional branch retention."""
    if disposition is CandidateDisposition.DISCARD:
        return False
    if disposition in {
        CandidateDisposition.PREREQUISITE,
        CandidateDisposition.PARETO_FRONTIER,
    }:
        return True
    return None


def _trusted_candidate_records(records: list[RoundRecord], space: MetricSpace) -> list[RoundRecord]:
    """Return reviewed checkpoints with complete comparable objective rows."""
    trusted: list[RoundRecord] = []
    for record in records:
        if not space.complete(_record_candidate_metrics(record)):
            continue
        if not record.commit or not record.passed or not record.reviewed:
            continue
        if not trusted_perf_provenance(record.perf_provenance):
            # An implementer self-reported headline metric may keep its commit
            # as a provisional claim, but it must never seed the archive as a
            # trusted Pareto parent or dominate later candidates.
            continue
        if _record_candidate_retained(record) is not True:
            continue
        trusted.append(record)
    return trusted


def _pareto_frontier_records(
    records: list[RoundRecord],
    space: MetricSpace,
) -> list[RoundRecord]:
    """Compute the noise-aware frontier over independently reviewed points."""
    return space.frontier(
        _trusted_candidate_records(records, space),
        _record_candidate_metrics,
    )


def _pareto_archive_dominators(
    candidate_metrics: dict[str, float],
    records: list[RoundRecord],
    space: MetricSpace,
) -> list[RoundRecord]:
    """Return trusted archive points that dominate a proposed candidate row."""
    if not space.complete(candidate_metrics):
        return []
    return [
        record
        for record in _trusted_candidate_records(records, space)
        if space.dominates(_record_candidate_metrics(record), candidate_metrics)
    ]


def _format_metric_row(metrics: dict[str, float], objectives: Sequence[Objective]) -> str:
    return ", ".join(
        f"{objective.name}={metrics[objective.name]:.6g} ({objective.direction})"
        for objective in objectives
        if objective.name in metrics
    )


def _pareto_archive_summary(records: list[RoundRecord], space: MetricSpace) -> str:
    """Render trusted frontier parents and any measured points awaiting review."""
    objectives = space.objectives
    latest = max(records, key=lambda record: record.round_number, default=None)
    latest_metrics = (
        _format_metric_row(latest.metrics, objectives)
        if latest is not None
        and latest.official_evaluation
        and trusted_perf_provenance(latest.perf_provenance)
        and objectives
        and space.complete(latest.metrics)
        else (
            f"{latest.perf_metric:.6g} {latest.perf_unit or ''}".strip()
            if latest is not None
            and latest.official_evaluation
            and trusted_perf_provenance(latest.perf_provenance)
            and latest.perf_metric is not None
            else "(none)"
        )
    )
    latest_line = (
        "Latest completed round: none."
        if latest is None
        else (
            f"Latest completed round: round {latest.round_number}, "
            f"commit {(latest.commit or '(missing)')[:12]}, "
            f"official metrics: {latest_metrics}; "
            f"retained: {_record_candidate_retained(latest)}."
        )
    )
    if not objectives:
        return (
            "No objective axes are configured. Use objectives.toml to enable "
            "multi-objective checkpoint retention; official scalar tracking remains active.\n"
            f"{latest_line}"
        )

    lines = [
        "Configured axes: "
        + ", ".join(f"{objective.name}:{objective.direction}" for objective in objectives),
        (
            "Dominance is variance-aware: a point removes another only when it is no worse "
            f"within {space.relative_noise:.0%} on every axis and better by more than "
            f"{space.relative_noise:.0%} on at least one."
        ),
        latest_line,
    ]
    frontier = _pareto_frontier_records(records, space)
    if frontier:
        lines.append("Trusted frontier parents:")
        for record in frontier:
            assert record.commit is not None  # noqa: S101  # tracked: #288
            evidence = "official" if record.official_evaluation else "reviewed provisional"
            operating_point = record.candidate_operating_point or "canonical workload row"
            artifact = record.candidate_evaluation_artifact or record.evaluation_artifact
            lines.append(
                f"- round {record.round_number}, commit {record.commit[:12]}, {evidence}: "
                f"{_format_metric_row(_record_candidate_metrics(record), objectives)}; "
                f"operating point: {operating_point}; artifact: {artifact or '(missing)'}"
            )
    else:
        lines.append("Trusted frontier parents: none recorded yet.")

    trusted_rounds = {record.round_number for record in _trusted_candidate_records(records, space)}
    pending = [
        record
        for record in records
        if record.round_number not in trusted_rounds
        and record.commit
        and _record_candidate_retained(record) is True
        and all(objective.name in record.candidate_metrics for objective in objectives)
    ]
    if pending:
        pending.sort(key=lambda record: record.round_number)
        lines.append(
            "Measured frontier claims not yet usable as trusted parents (retain the commit, "
            "but do not treat it as a parent). A row lands here because its hard invariants "
            "have not passed independent review, or because its numbers are the "
            "implementer's own report rather than a framework measurement:"
        )
        omitted = pending[:-_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT]
        if omitted:
            # This line is read by a model, so it agrees with itself: one
            # omitted claim says "1 older untrusted claim", and a single
            # omitted round says "round 4" rather than the degenerate
            # "rounds 4-4".
            claims = "claim" if len(omitted) == 1 else "claims"
            first = omitted[0].round_number
            last = omitted[-1].round_number
            rounds = f"round {first}" if first == last else f"rounds {first}-{last}"
            lines.append(
                f"- {len(omitted)} older untrusted {claims} omitted from this context "
                f"({rounds}); do not treat any omitted claim as a trusted parent."
            )
        for record in pending[-_PARETO_ARCHIVE_PENDING_CLAIM_LIMIT:]:
            assert record.commit is not None  # noqa: S101  # tracked: #288
            lines.append(
                f"- round {record.round_number}, commit {record.commit[:12]}: "
                f"{_format_metric_row(record.candidate_metrics, objectives)}; "
                f"operating point: {record.candidate_operating_point or '(unspecified)'}; "
                f"artifact: {record.candidate_evaluation_artifact or '(missing)'}; "
                f"reason: {record.candidate_retention_reason or '(unspecified)'}"
            )
    return "\n".join(lines)


def _headline_measurement(record: RoundRecord) -> Measurement | None:
    """Return a record's scalar headline as a typed measurement."""
    if record.perf_metric is None or record.perf_unit is None:
        return None
    return Measurement(
        metric=record.perf_unit,
        value=record.perf_metric,
        direction=record.perf_direction,
    )


def _trusted_final_records(records: list[RoundRecord], space: MetricSpace) -> list[RoundRecord]:
    """Return retained records with canonical framework-owned measurements."""
    return [
        record
        for record in records
        if record.commit
        and record.passed
        and record.reviewed
        and record.official_evaluation
        and trusted_perf_provenance(record.perf_provenance)
        and _record_candidate_retained(record) is True
        and (
            space.complete(record.metrics)
            if space.objectives
            else space.direction(_headline_measurement(record)) is not None
        )
    ]


def _select_final_candidate(records: list[RoundRecord], space: MetricSpace) -> RoundRecord | None:
    """Select the latest noise-aware winner from trusted retained records."""
    newest_first = sorted(
        _trusted_final_records(records, space),
        key=lambda record: record.round_number,
        reverse=True,
    )
    if space.primary is not None:
        frontier_rounds = {
            record.round_number for record in _pareto_frontier_records(newest_first, space)
        }
        candidates = [record for record in newest_first if record.round_number in frontier_rounds]
        primary = space.primary

        def primary_measurement(record: RoundRecord) -> Measurement:
            return Measurement(
                metric=primary.name,
                value=record.metrics[primary.name],
                direction=primary.direction,
            )

        return space.best(candidates, primary_measurement)
    return space.best(newest_first, _headline_measurement)


def _finalize_agent_run(
    ctx: LoopContext,
    *,
    records: list[RoundRecord],
    space: MetricSpace,
    progress_path: Path,
) -> None:
    """Persist the final archive, report trusted results, and restore the winner."""
    issue_board.write_pareto_archive(progress_path, _pareto_archive_summary(records, space))
    if space.objectives:
        frontier = _pareto_frontier_records(records, space)
        ctx.lprint(f"\nFinal Pareto frontier ({len(frontier)} rounds):")
        for record in frontier:
            ctx.lprint(
                f"  round {record.round_number}: "
                f"{_format_metric_row(_record_candidate_metrics(record), space.objectives)} "
                f"(commit {(record.commit or 'n/a')[:12]})"
            )

    winner = _select_final_candidate(records, space)
    relative_memory = tuple(
        str(path.relative_to(ctx.workspace))
        for path in issue_board.framework_memory_paths(ctx.workspace)
    )
    if winner is None:
        baseline = ctx.git.trusted_input_baseline
        if baseline is None:
            raise RuntimeError(  # noqa: TRY003
                "no trusted retained candidate or trusted input baseline is available"
            )
        if not ctx.git.checkout_tree(baseline, clean=True, preserve_paths=relative_memory):
            raise RuntimeError(  # noqa: TRY003
                f"could not restore trusted input baseline at {baseline}"
            )
        ctx.snapshot_workspace("agent: restore trusted input baseline")
        ctx.lprint(
            f"\nNo evaluated winner was retained. Restored trusted input baseline {baseline[:12]}."
        )
        return
    assert winner.commit is not None  # noqa: S101  # selected records require a commit
    ctx.git.retain_candidate(f"selected-round-{winner.round_number:04d}", winner.commit)
    if not ctx.git.checkout_tree(winner.commit, clean=True, preserve_paths=relative_memory):
        raise RuntimeError(  # noqa: TRY003
            f"could not materialize selected round {winner.round_number} at {winner.commit}"
        )
    ctx.snapshot_workspace(f"agent: select round {winner.round_number}")
    metrics = (
        _format_metric_row(_record_candidate_metrics(winner), space.objectives)
        if space.objectives
        else f"{winner.perf_metric:.6g} {winner.perf_unit or ''}"
    )
    ctx.lprint(
        f"\nFinal selected candidate: round {winner.round_number}, "
        f"commit {winner.commit[:12]}, official metrics: {metrics.strip()}"
    )


def _pareto_archive_conflict(
    *,
    candidate_disposition: CandidateDisposition,
    candidate_metrics: dict[str, float],
    records: list[RoundRecord],
    space: MetricSpace,
) -> str | None:
    """Explain why a claimed frontier row is dominated by the live archive."""
    if candidate_disposition is not CandidateDisposition.PARETO_FRONTIER:
        return None
    dominators = _pareto_archive_dominators(candidate_metrics, records, space)
    if not dominators:
        return None
    rows = "; ".join(
        f"round {record.round_number} ({_format_metric_row(_record_candidate_metrics(record), space.objectives)})"
        for record in dominators
    )
    return (
        "The candidate's `pareto_frontier` disposition conflicts with the live "
        f"noise-aware archive: it is dominated by {rows}. A numeric archive gate "
        "frozen into the hypothesis plan does not override the current archive. "
        "Report this row as `discard` unless its metrics or configured objective "
        "comparability were recorded incorrectly; do not rerun an unchanged "
        "candidate merely to repair the disposition."
    )


# ---------------------------------------------------------------------------
# Plateau detection
# ---------------------------------------------------------------------------


_PLATEAU_THRESHOLD_PCT = 5.0
_PLATEAU_MIN_STREAK = 3


def _detect_plateau(
    records: list[RoundRecord],
    *,
    threshold_pct: float = _PLATEAU_THRESHOLD_PCT,
    min_streak: int = _PLATEAU_MIN_STREAK,
) -> str | None:
    """Return a warning string if the most recent ``min_streak`` rounds
    with **fresh, same-unit** perf metrics stayed within ``threshold_pct``
    of each other; else None.

    Rules:
    - ``profile_skipped`` rounds don't count as fresh measurements (their
      perf was reused from earlier).
    - Only rounds the framework measured itself count. An implementer's
      self-reported number is not evidence that the search has stopped
      making progress, and telling the orchestrator it has plateaued on
      the strength of its own reports is a feedback loop.
    - Only rounds with the *same* ``perf_unit`` as the latest fresh round
      count toward the streak — comparing latency_ms against tok/s as raw
      floats is a category error.
    - Failed rounds (``passed=False`` or no perf_metric) are stepped over.

    The orchestrator gets this verbatim in its prompt; phrasing is
    user-facing.
    """  # noqa: D205  # tracked: #288
    fresh = [
        r
        for r in records
        if r.passed
        and r.official_evaluation
        and r.perf_metric is not None
        and trusted_perf_provenance(r.perf_provenance)
        and not r.profile_skipped
    ]
    if len(fresh) < min_streak:
        return None
    latest_unit = fresh[-1].perf_unit
    same_unit = [r for r in fresh if r.perf_unit == latest_unit]
    if len(same_unit) < min_streak:
        return None
    tail = same_unit[-min_streak:]
    perfs = [r.perf_metric for r in tail if r.perf_metric is not None]
    hi = max(perfs)
    lo = min(perfs)
    if hi <= 0:
        return None
    spread_pct = (hi - lo) / hi * 100
    if spread_pct >= threshold_pct:
        return None
    unit_suffix = f" {latest_unit}" if latest_unit else ""
    rounds = [r.round_number for r in tail]
    return (
        f"The last {min_streak} rounds with a fresh perf measurement (rounds "
        f"{rounds[0]}–{rounds[-1]}) all landed in {lo:.2f}–{hi:.2f}{unit_suffix} "  # noqa: RUF001  # tracked: #288
        f"— a {spread_pct:.2f}% spread, well within bench noise. Whatever you've "
        f"been working on for those rounds is not actually moving the headline "
        f"metric."
    )


# ---------------------------------------------------------------------------
# Carry-over state between rounds
# ---------------------------------------------------------------------------


@dataclass
class _CarryOver:
    regression_info: str | None = None
    exhaustion_info: str | None = None


def _review_due(
    *,
    round_number: int,
    max_rounds: int,
    judge_every: int,
    outcome: HypothesisOutcome,
    candidate_evidence_fresh: bool = False,
) -> bool:
    """Return whether an independent review must run for this candidate.

    A fresh objective row is itself a checkpoint-retention claim. Review it
    even when the implementer labels the checkpoint ``prerequisite`` or
    ``discard`` so a mistaken disposition cannot bypass the independent judge
    and disappear from Pareto memory. The judge can audit the existing raw
    artifact without requiring another benchmark run.

    Repeating an already-recorded row is not fresh evidence and therefore does
    not bypass sparse review cadence.
    """
    return (
        round_number == max_rounds
        or round_number % judge_every == 0
        or outcome in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
        or candidate_evidence_fresh
    )


def _candidate_evidence_is_fresh(
    implementation: ImplementerResponse,
    records: list[RoundRecord],
) -> bool:
    """Return whether an implementer reported a previously unseen objective row."""
    if not implementation.candidate_metrics:
        return False
    artifact = implementation.candidate_evaluation_artifact
    if not artifact:
        # Missing provenance is still a new claim that needs prompt review.
        return True
    metrics = dict(implementation.candidate_metrics)
    return not any(
        (record.candidate_evaluation_artifact or record.evaluation_artifact) == artifact
        and record.candidate_metrics == metrics
        for record in records
    )


def _provisional_candidates_since_official(records: list[RoundRecord]) -> int:
    """Count accepted candidate checkpoints after the latest official one."""
    count = 0
    for record in reversed(records):
        if record.official_evaluation:
            break
        if (
            record.passed
            and record.reviewed
            and (
                _record_candidate_retained(record) is True
                # An accepted-but-unmeasured hypothesis consumes cadence budget
                # like a proven one: it is exactly the checkpoint the next
                # official evaluation must measure.
                or record.hypothesis_outcome
                in {
                    HypothesisResolution.PROVEN.value,
                    HypothesisResolution.UNMEASURED.value,
                }
            )
        ):
            count += 1
    return count


def _official_evaluation_reason(  # noqa: PLR0913  # tracked: #288
    *,
    records: list[RoundRecord],
    round_number: int,
    max_rounds: int,
    official_eval_every: int,
    requested: bool,
    candidate_ready: bool,
) -> str | None:
    """Return why framework-owned gates should run for the working head.

    Cadence counts accepted candidate checkpoints rather than raw framework
    rounds. Retries, continuing hypotheses, profiling passes, and rejected
    changes therefore cannot accidentally consume the expensive-evaluation
    budget.
    """
    if round_number == max_rounds:
        return "final_round"
    if not candidate_ready:
        return None
    if requested:
        return "orchestrator_request"
    provisional = _provisional_candidates_since_official(records)
    if provisional + 1 >= official_eval_every:
        return "cadence"
    return None


def _terminal_workspace_notice(records: list[RoundRecord]) -> str | None:
    """Describe a terminal hypothesis whose edits remain in the workspace."""
    if not records:
        return None
    latest = records[-1]
    terminal_outcomes = {
        HypothesisOutcome.DISPROVEN.value,
        HypothesisOutcome.IMPLEMENTATION_FAILED.value,
        HypothesisOutcome.INCONCLUSIVE.value,
        HypothesisOutcome.BLOCKED.value,
    }
    if latest.hypothesis_outcome not in terminal_outcomes:
        return None

    if _record_candidate_retained(latest) is True:
        review_status = (
            "independently reviewed"
            if latest.passed and latest.reviewed
            else "awaiting independent review"
        )
        return (
            f"Hypothesis `{latest.hypothesis_id or 'unspecified'}` ended as "
            f"`{latest.hypothesis_outcome}` in round {latest.round_number}, but its "
            f"implementation reported a {review_status} Pareto checkpoint: "
            f"{latest.candidate_metrics or '(metrics missing)'}. Preserve commit "
            f"`{(latest.commit or '(missing)')[:12]}` as a distinct branch candidate. "
            "The causal forecast and checkpoint retention decision are separate: do "
            "not erase a credible throughput/latency tradeoff merely because another "
            "axis or the forecast missed. If review is pending, validate hard "
            "correctness and workload invariants before using it as a trusted parent. "
            "Choose this checkpoint only when the next hypothesis names which frontier "
            "gap it will improve; otherwise explicitly restore another frontier parent."
        )

    campaign_records = [latest]
    for record in reversed(records[:-1]):
        if record.hypothesis_id != latest.hypothesis_id:
            break
        campaign_records.append(record)
    campaign_records.reverse()
    started_round = campaign_records[0].round_number
    parent_round = next(
        (
            record.hypothesis_parent_round
            for record in campaign_records
            if record.hypothesis_parent_round is not None
        ),
        started_round - 1 if started_round > 1 else None,
    )
    parent_guidance = (
        f"The recorded pre-hypothesis parent is round {parent_round}; use "
        f"`revert_to_round={parent_round}` if that parent should be restored."
        if parent_round is not None
        else "No earlier recorded round exists, so identify the clean parent state explicitly."
    )
    latest_checkpoint = next(
        (
            record
            for record in reversed(records[:-1])
            if record.commit is not None
            and record.hypothesis_outcome in {HypothesisOutcome.CONTINUE.value, "proven"}
        ),
        None,
    )
    checkpoint_guidance = ""
    if latest_checkpoint is not None and latest_checkpoint.round_number != parent_round:
        review_label = "reviewed" if latest_checkpoint.reviewed else "provisional"
        checkpoint_guidance = (
            " The most recent earlier nonterminal checkpoint is round "
            f"{latest_checkpoint.round_number} "
            f"(`{latest_checkpoint.hypothesis_outcome}`, {review_label}). If the "
            "terminal evidence rejects only the newest child experiment, preserve "
            "that checkpoint instead of discarding prior gains; restore the original "
            "pre-hypothesis parent only when the evidence invalidates the full chain. "
            f"If metrics from round {latest_checkpoint.round_number} are the "
            "restoration gate, restore that checkpoint or preserve all production "
            "changes through it. An older implementation cannot be required to "
            "reproduce a later checkpoint's metric while those later gains are omitted."
        )
    return (
        f"Hypothesis `{latest.hypothesis_id or 'unspecified'}` ended as "
        f"`{latest.hypothesis_outcome}` in round {latest.round_number}, but its "
        "workspace edits are still present. Before building a new hypothesis, "
        "decide explicitly whether to roll those edits back or retain a reusable "
        "correctness/measurement prerequisite. Do not silently build on a "
        f"falsified performance mechanism. {parent_guidance}{checkpoint_guidance} "
        "If retaining any "
        "part, justify it and re-establish the end-to-end parent behavior."
    )


# ---------------------------------------------------------------------------
# Round phases
# ---------------------------------------------------------------------------


def _is_fresh_cold_start(round_number: int, records: list[RoundRecord]) -> bool:
    """True for round 1 of a fresh run (no prior rounds recorded)."""
    return round_number == 1 and not records


def _run_pre_round_decision(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    agent: SharedAgentHandle,
    round_number: int,
    objective: str,
    carry: _CarryOver,
    progress_path: Path,
    progress_location: str,
    has_history: bool = True,
) -> PreRoundDecision:
    system_prompt = render_template(
        "orchestrator_pre_round_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        objective=objective,
        objective_location=ctx.objective_location,
        regression_info=carry.regression_info,
        exhaustion_info=carry.exhaustion_info,
        progress_location=progress_location,
        profiler_kind=ctx.profiler_kind.value,
        profile_execution=ctx.run_environment_view.profile_execution,
        has_history=has_history,
    )
    decision = _invoke_read_only_role(
        ctx,
        agent=agent,
        role="orchestrator",
        checkpoint_label=f"round-{round_number}-pre-input",
        kind="orchestrator",
        system_prompt=system_prompt,
        user_prompt=(
            "Decide whether a profiling pass is needed before planning "
            "this round. Return only the JSON object."
        ),
        response_cls=PreRoundDecision,
        fallback_factory=lambda: PreRoundDecision(
            need_profile=False,
            profile_focus="",
            reasoning="fallback: default to skip",
        ),
        round_label=f"round-{round_number}-pre",
        reuse_session=False,
    )
    issue_board.append_pre_round_decision(progress_path, round_number, decision)
    return decision


def _profiler_prompt_template(
    profiler_kind: ProfilerKind,
    *,
    supports_torch_profiler: bool = False,
) -> str:
    """Pick the prompt for the profiler resolved during context creation."""
    return _effective_profiler_definition(
        profiler_kind,
        supports_torch_profiler=supports_torch_profiler,
    ).prompt_template


def _effective_profiler_definition(  # noqa: ANN202  # tracked: #288
    profiler_kind: ProfilerKind,
    *,
    supports_torch_profiler: bool = False,
):
    """Return the already-resolved profiler declaration.

    Context creation resolves the requested profiler against both the domain
    and the run environment's declared capabilities.  Do not perform a second
    interface-based substitution here: it can replace a supported remote
    capture path with a profiler that the environment cannot execute.
    """
    kind = require_profiler_kind(profiler_kind)
    if kind is ProfilerKind.NONE:
        raise ValueError("No profiler prompt exists when profiling is disabled.")  # noqa: TRY003  # tracked: #288
    definition = profiler_definition(kind)
    if definition.requires_domain_torch_support and not supports_torch_profiler:
        raise ValueError("The selected domain does not provide Torch profiler support.")  # noqa: TRY003  # tracked: #288
    return definition


def _run_profiler(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    agent: SharedAgentHandle,
    round_number: int,
    profile_focus: str,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
    progress_path: Path,
    objective: str,
) -> ProfilerSummary | None:
    template = _profiler_prompt_template(
        ctx.profiler_kind,
        supports_torch_profiler=domain_definition.supports_torch_profiler,
    )
    domain_profiler = render_domain_section(
        domain_definition,
        DomainRole.PROFILER,
        **_domain_render_context(ctx, modality, interface),
    )
    system_prompt = render_template(
        template,
        template_dir=_TEMPLATE_DIR,
        profile_focus=profile_focus,
        benchmark_command=ctx.profiler_benchmark_command,
        modality=modality,
        domain_profiler=domain_profiler,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        profile_execution=ctx.run_environment_view.profile_execution,
        objective=objective,
        profiler_support_name=profiler_definition(ctx.profiler_kind).support_name,
        profiler_mcp_name=profiler_definition(ctx.profiler_kind).mcp_name,
    )
    progress_location = issue_board.display_path(progress_path, ctx.workspace)
    profiler_artifact_path = issue_board.profiler_artifact_root(progress_path, round_number)
    profiler_artifact_location = issue_board.display_path(
        profiler_artifact_path, ctx.workspace
    ).rstrip("/")
    system_prompt += f"""

## Recent campaign context

The durable progress artifact is `{progress_location}`. Inspect the most recent
applicable round with tools to identify the current candidate, hypothesis, and
retained evaluation artifacts. Read older rounds only when the requested focus
depends on them.

For the requested profile focus, resolve artifacts explicitly referenced by
the most recent applicable round before considering older similarly named
artifacts. Do not launch a duplicate expensive evaluation when retained
current-candidate evidence already answers the focus; collect the smallest
additional profile that closes a specific evidence gap instead.

## Read-only evidence boundary

Use only capture interfaces present when this turn started. Never edit or add
candidate source, configuration, tests, locks, instrumentation, endpoints, or
entrypoints. If the requested production path is not observable, report that
capability mismatch; a later Implementer may add reviewed instrumentation.

Write bounded durable profile evidence only below
`{profiler_artifact_location}/`; keep large transient traces under `/tmp`.
"""
    spec = profiler_mcp_spec(ctx.profiler_kind)
    try:
        summary = _invoke_read_only_role(
            ctx,
            agent=agent,
            role="profiler",
            checkpoint_label=f"round-{round_number}-profiler-input",
            allowed_workspace_paths=(profiler_artifact_location,),
            kind="profiler",
            system_prompt=system_prompt,
            user_prompt=(
                "Profile the server and return exactly one JSON object matching the schema above."
            ),
            response_cls=ProfilerSummary,
            fallback_factory=lambda: ProfilerSummary(
                analysis="Profiler produced no structured response.",
                bottlenecks="n/a",
                suggestions="Re-run profiling on the next round.",
                perf_metric=None,
                perf_unit=None,
            ),
            round_label=f"round-{round_number}-profiler",
            mcp_servers=[spec] if spec is not None else None,
        )
    except Exception as exc:  # noqa: BLE001  # tracked: #288
        output_sink().framework_warning(
            "profiler failed",
            detail=str(exc),
            source=FrameworkSource.LOOP,
            round_label=f"round-{round_number}",
        )
        return None
    if summary is None:
        return None
    issue_board.append_profiler_summary(progress_path, round_number, summary)
    ctx.snapshot_workspace(f"round-{round_number}-profiler")
    return summary


def _domain_render_context(
    ctx: LoopContext, modality: str | None, interface: str
) -> dict[str, object]:
    """The uniform variable set every domain role file is rendered with.

    One context contract for all roles: a pack author can branch (``{% if … %}``)
    on any of these in any role file without memorizing which the loop happens
    to pass to which role. Variables that don't apply to the current run are
    falsy (``benchmark_command`` / ``accuracy_command`` when nothing is attached),
    so ``{% if benchmark_command %}`` works everywhere. ``interface`` lets a
    domain distinguish direct invocation from an over-the-wire service without
    treating that boundary as a language choice. See ``docs/contributing/domains.md``.
    """
    return {
        "modality": modality,
        "interface": interface,
        "reference_path": ctx.ref_name,
        "benchmark_command": ctx.judge_benchmark_command,
        "accuracy_command": ctx.judge_accuracy_command,
        "runtime_notes": ctx.run_environment_view.prompt_notes,
        "profile_execution": ctx.run_environment_view.profile_execution,
        "workspace_sources": ctx.workspace_sources,
    }


def _run_orchestrator_plan(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    agent: SharedAgentHandle,
    agent_run_state: AgentRunState,
    round_number: int,
    objective: str,
    profiler_summary: ProfilerSummary | None,
    carry: _CarryOver,
    progress_path: Path,
    progress_location: str,
    roadmap_location: str,
    pareto_archive_location: str,
    plateau_warning: str | None,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
    framework_benchmark_enabled: bool = False,
    official_eval_every: int = 3,
    provisional_candidates: int = 0,
    official_eval_cadence_due: bool = False,
    profile_guidance: ProfileGuidanceView | None = None,
) -> OrchestratorPlan:
    domain_orchestrator = render_domain_section(
        domain_definition,
        DomainRole.ORCHESTRATOR,
        **_domain_render_context(ctx, modality, interface),
    )
    system_prompt = render_template(
        "orchestrator_plan_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        objective=objective,
        objective_location=ctx.objective_location,
        profiler_summary=profiler_summary,
        regression_info=carry.regression_info,
        exhaustion_info=carry.exhaustion_info,
        progress_location=progress_location,
        roadmap_location=roadmap_location,
        pareto_archive_location=pareto_archive_location,
        plateau_warning=plateau_warning,
        domain_orchestrator=domain_orchestrator,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        profile_execution=ctx.run_environment_view.profile_execution,
        framework_benchmark_enabled=framework_benchmark_enabled,
        official_eval_every=official_eval_every,
        provisional_candidates=provisional_candidates,
        official_eval_cadence_due=official_eval_cadence_due,
        **(profile_guidance.plan_prompt_context() if profile_guidance else {}),
    )
    # One corrective reprompt: a plan that fails lifecycle validation (for
    # example a hypothesis_id already used in this run) is a recoverable agent
    # mistake, not a framework invariant violation.
    #
    # A rejected attempt writes no plan artifact and no progress note -- both
    # happen after validation -- and leaves durable hypothesis state untouched,
    # because `_validate_orchestrator_plan_state` applies strategy updates to a
    # clone it discards. It is not a full rollback, though: the orchestrator's
    # one allowlisted write, the roadmap index, is preserved by
    # `_invoke_read_only_role` rather than reverted, so roadmap edits the
    # rejected attempt made survive into the retry. That is deliberate. The
    # roadmap is the orchestrator's own long-lived planning document, and the
    # thinking it recorded there is not invalidated by the plan JSON being
    # rejected for an identifier collision.
    corrective_feedback: str | None = None
    attempt = 0
    while True:
        attempt += 1
        # `round-N-plan`, then `round-N-retry-1-plan`. Both the client's
        # planning-stage matcher and `_attempt_from_label` parse this shape, so
        # a reprompted plan still appears as a planning activity and still
        # reports which attempt produced it.
        label = f"round-{round_number}" + (f"-retry-{attempt - 1}" if attempt > 1 else "") + "-plan"
        plan = _invoke_read_only_role(
            ctx,
            agent=agent,
            role="orchestrator",
            checkpoint_label=f"{label}-input",
            allowed_workspace_paths=(
                f"{roadmap_location.rstrip('/')}/index.md"
                if roadmap_location.endswith("/")
                else roadmap_location,
            ),
            kind="orchestrator",
            system_prompt=system_prompt,
            user_prompt=(
                corrective_feedback or "Produce this round's plan. Return only the JSON object."
            ),
            response_cls=OrchestratorPlan,
            fallback_factory=lambda: OrchestratorPlan(
                task="Re-check minimal server boots and /health returns 200.",
                pass_criteria="/health returns 200.",  # noqa: S106  # tracked: #288
                reasoning="fallback: orchestrator produced no structured response",
            ),
            round_label=label,
            reuse_session=False,
        )
        plan.hypothesis_id = plan.hypothesis_id.strip() or f"hypothesis-{round_number:04d}"
        plan.title = normalize_hypothesis_title(plan.title)
        try:
            _validate_orchestrator_plan_state(plan, agent_run_state)
        except ValueError as error:
            if corrective_feedback is not None:
                raise
            ctx.lprint(f"[orchestrator] plan rejected ({error}); reprompting once")
            rejected_updates = ", ".join(
                sorted({update.hypothesis_id for update in plan.hypothesis_updates})
            )
            corrective_feedback = (
                f"Your previous plan was rejected: {error}. "
                f"It proposed hypothesis_id {plan.hypothesis_id!r} and named "
                f"{rejected_updates or '(no)'} in hypothesis_updates. "
                "A hypothesis_id names one investigation permanently: never reuse "
                "an identifier used earlier in this run, and choose one that has "
                "not appeared before. hypothesis_updates may name each prior "
                "hypothesis at most once, and never the new one. "
                "Produce a corrected plan for this round. "
                "Return only the JSON object."
            )
            continue
        plan.recommended_skills, _ = _validate_skill_selections(ctx, plan.recommended_skills)
        issue_board.write_plan_artifact(progress_path, round_number, plan)
        issue_board.append_orchestrator_plan(progress_path, round_number, plan)
        return plan


def _validate_orchestrator_plan_state(
    plan: OrchestratorPlan,
    state: AgentRunState,
) -> None:
    """Validate structured lifecycle decisions against framework-owned state."""
    if len({update.hypothesis_id for update in plan.hypothesis_updates}) != len(
        plan.hypothesis_updates
    ):
        raise ValueError(  # noqa: TRY003  # tracked: #288
            "Orchestrator hypothesis_updates must name each hypothesis once"
        )
    if any(update.hypothesis_id == plan.hypothesis_id for update in plan.hypothesis_updates):
        raise ValueError(  # noqa: TRY003  # tracked: #288
            "Orchestrator hypothesis_updates must refer to prior hypotheses"
        )
    if state.by_id(plan.hypothesis_id) is not None:
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"hypothesis ID {plan.hypothesis_id!r} was already used"
        )
    # Apply to a copy before any plan artifact is written. The real transition
    # is persisted after the operational active checkpoint is assembled.
    apply_strategy_updates(state, plan.hypothesis_updates)


def _missing_implementer_response() -> ImplementerResponse:
    """Fail closed when an implementer turn does not match its response schema."""
    return ImplementerResponse(
        summary="Implementer produced no structured response.",
        expected_behavior="unknown",
        hypothesis_outcome=HypothesisOutcome.INCONCLUSIVE,
        evidence="The implementer output could not be parsed as ImplementerResponse.",
        next_step=(
            "Recover the retained workspace evidence and return a schema-valid "
            "ImplementerResponse before requesting review or official evaluation."
        ),
    )


def _timed_out_implementer_response(timeout_seconds: float) -> ImplementerResponse:
    """Fail closed while retaining durable evidence for a timed-out turn."""
    return ImplementerResponse(
        summary="Implementer invocation timed out.",
        expected_behavior="unknown",
        hypothesis_outcome=HypothesisOutcome.INCONCLUSIVE,
        evidence=(
            "The framework stopped the implementer after "
            f"{timeout_seconds:g} seconds without a structured response."
        ),
        next_step=(
            "Inspect the retained workspace and prior-attempt artifact, then return "
            "a schema-valid ImplementerResponse on the configured retry."
        ),
    )


@dataclass(frozen=True)
class _ImplementerAttempt:
    """One implementer turn plus who authored its response.

    ``synthesized`` is True when the framework had to build the response with
    :func:`_missing_implementer_response` because the turn's output did not
    parse. Such a turn produced no reviewable evidence, so it must consume a
    same-round retry rather than complete the round like a genuine
    ``inconclusive`` result would.
    """

    response: ImplementerResponse
    synthesized: bool


def _validate_skill_selections(
    ctx: LoopContext,
    selections: list[SkillResourceSelection],
) -> tuple[list[SkillResourceSelection], list[ResolvedSkillSelection]]:
    """Validate advisory skill paths before a role handoff.

    Plans retain skill-relative paths so they can be resolved again after a
    resumed run materializes skills in a fresh sandbox.  Prompts receive only
    the corresponding agent-visible paths.  Invalid recommendations are
    diagnostic, never a reason to abort an otherwise useful experiment.
    """
    if not selections:
        return [], []
    skill_sources = ctx.skill_source_paths
    if not skill_sources:
        output_sink().framework_warning(
            "ignored skill recommendations because no skills are installed",
            source=FrameworkSource.LOOP,
            source_label="skills",
        )
        return [], []
    try:
        catalog = build_skill_catalog(skill_sources)
        resolved, diagnostics = resolve_skill_selections(selections, catalog)
    except (OSError, ValueError) as exc:
        output_sink().framework_warning(
            "ignored skill recommendations because the catalog is invalid",
            detail=f"{type(exc).__name__}: {exc}",
            source=FrameworkSource.LOOP,
            source_label="skills",
        )
        return [], []
    for diagnostic in diagnostics:
        output_sink().framework_warning(
            diagnostic,
            source=FrameworkSource.LOOP,
            source_label="skills",
        )

    validated = [
        SkillResourceSelection(
            skill=selection.skill,
            resource_paths=[
                path.removeprefix(f"{selection.skill}/") for path in selection.resource_paths
            ],
            purpose=selection.purpose,
        )
        for selection in resolved
    ]
    return validated, resolved


def _run_implementer(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    agent: SharedAgentHandle,
    round_number: int,
    retry: int,
    plan: OrchestratorPlan,
    objective: str,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
    feedback: str | None,
    continuation_step: str | None,
    framework_revert_applied: bool,
    framework_revert_round: int | None,
    framework_revert_commit: str | None,
    progress_path: Path,
    progress_location: str,
    pareto_archive_location: str,
    gate_revalidation_pending: bool = False,
    gate_approved_perf_metric: float | None = None,
    gate_approved_perf_unit: str | None = None,
    gate_approved_evaluation_artifact: str | None = None,
    framework_benchmark_enabled: bool = False,
    official_evaluation_due: bool = False,
    official_evaluation_reason: str | None = None,
    prior_attempt_artifact_locations: tuple[str, ...] = (),
    profile_guidance: ProfileGuidanceView | None = None,
) -> _ImplementerAttempt:
    plan.recommended_skills, resolved_skills = _validate_skill_selections(
        ctx, plan.recommended_skills
    )
    plan_artifact = issue_board.write_plan_artifact(progress_path, round_number, plan)
    plan_artifact_location = issue_board.display_path(plan_artifact, ctx.workspace)
    validation_location = issue_board.display_path(
        issue_board.validation_artifact_root(progress_path), ctx.workspace
    )
    validation_recipe_contract_location = issue_board.display_path(
        issue_board.validation_recipe_schema_path(progress_path), ctx.workspace
    )
    domain_implementer = render_domain_section(
        domain_definition,
        DomainRole.IMPLEMENTER,
        **_domain_render_context(ctx, modality, interface),
    )
    system_prompt = render_template(
        ("implementer_continuation_prompt.j2" if continuation_step else "implementer_prompt.j2"),
        template_dir=_TEMPLATE_DIR,
        reference_path=ctx.ref_name,
        modality=modality,
        interface=interface,
        domain_implementer=domain_implementer,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        objective=objective,
        objective_location=ctx.objective_location,
        plan_artifact_location=plan_artifact_location,
        hypothesis_id=plan.hypothesis_id,
        hypothesis=plan.hypothesis,
        activation_evidence=plan.activation_evidence,
        falsification_criteria=plan.falsification_criteria,
        expected_effect=plan.expected_effect,
        minimum_acceptance_criteria=plan.minimum_acceptance_criteria,
        invariants=plan.invariants,
        progress_location=progress_location,
        pareto_archive_location=pareto_archive_location,
        validation_location=validation_location,
        validation_recipe_contract_location=validation_recipe_contract_location,
        retry=retry,
        feedback=feedback,
        continuation_step=continuation_step,
        framework_revert_applied=framework_revert_applied,
        framework_revert_round=framework_revert_round,
        framework_revert_commit=framework_revert_commit,
        gate_revalidation_pending=gate_revalidation_pending,
        gate_approved_perf_metric=gate_approved_perf_metric,
        gate_approved_perf_unit=gate_approved_perf_unit,
        gate_approved_evaluation_artifact=gate_approved_evaluation_artifact,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        profile_execution=ctx.run_environment_view.profile_execution,
        framework_benchmark_enabled=framework_benchmark_enabled,
        official_evaluation_due=official_evaluation_due,
        official_evaluation_reason=official_evaluation_reason,
        recommended_skills=resolved_skills,
        prior_attempt_artifact_locations=prior_attempt_artifact_locations,
        **(profile_guidance.implementer_prompt_context() if profile_guidance else {}),
    )
    # Make this attempt number durable before the turn starts. A process killed
    # mid-invoke writes no completed artifact, so a resume that counted only
    # those would reuse this attempt number and replay the round label below
    # over paid work.
    issue_board.write_implementer_start_marker(progress_path, round_number, retry)
    fallback = ResponseFallback(_missing_implementer_response)
    timed_out = False
    try:
        response = agent.turn_structured(
            (
                "Execute the required continuation step for the active hypothesis; "
                "do not merely restate prior work. Return only the JSON object."
                if continuation_step
                else "Work persistently on the active hypothesis and return only the JSON object."
            ),
            system_prompt=system_prompt,
            response_cls=ImplementerResponse,
            fallback_factory=fallback,
            label=f"round-{round_number}-retry-{retry}-implementer",
            reuse_session=True,
            session_key=AgentSessionKey(SessionScope.HYPOTHESIS, plan.hypothesis_id),
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        response = _timed_out_implementer_response(exc.timeout)
        ctx.lprint(
            f"[implementer] attempt {retry} timed out after {exc.timeout:g} seconds; "
            "persisting fail-closed evidence."
        )
    response.skill_context_updates, _ = _validate_skill_selections(
        ctx, response.skill_context_updates
    )
    if response.skill_context_updates:
        plan.recommended_skills, _ = _validate_skill_selections(
            ctx, [*plan.recommended_skills, *response.skill_context_updates]
        )
        issue_board.write_plan_artifact(progress_path, round_number, plan)
    issue_board.write_implementer_artifact(progress_path, round_number, retry, response)
    issue_board.append_implementer(progress_path, round_number, retry, response)
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-implementer")
    return _ImplementerAttempt(
        response=response,
        synthesized=fallback.synthesized or timed_out,
    )


def _run_judge(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    agent: SharedAgentHandle,
    round_number: int,
    retry: int,
    plan: OrchestratorPlan,
    implementation: ImplementerResponse,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
    progress_path: Path,
    progress_location: str,
    pareto_archive_location: str,
    objective: str,
    framework_revert_applied: bool,
    framework_revert_round: int | None,
    framework_revert_commit: str | None,
    gate_revalidation_pending: bool = False,
    gate_approved_perf_metric: float | None = None,
    gate_approved_perf_unit: str | None = None,
    gate_approved_metrics: dict[str, float] | None = None,
    gate_approved_evaluation_artifact: str | None = None,
    framework_benchmark_enabled: bool = False,
    official_evaluation_due: bool = False,
    official_evaluation_reason: str | None = None,
    pareto_archive_conflict: str | None = None,
) -> JudgeResponse:
    # Rewrite both handoffs from the framework's parsed in-memory objects just
    # before review. Candidate code may write the shared workspace, so the
    # Judge must receive paths to fresh framework-owned records rather than
    # interpolated free-form implementer prose.
    plan_artifact = issue_board.write_plan_artifact(progress_path, round_number, plan)
    implementer_artifact = issue_board.write_implementer_artifact(
        progress_path, round_number, retry, implementation
    )
    plan_artifact_location = issue_board.display_path(plan_artifact, ctx.workspace)
    implementer_artifact_location = issue_board.display_path(implementer_artifact, ctx.workspace)
    validation_location = issue_board.display_path(
        issue_board.validation_artifact_root(progress_path), ctx.workspace
    )
    validation_recipe_contract_location = issue_board.display_path(
        issue_board.validation_recipe_schema_path(progress_path), ctx.workspace
    )
    judge_domain_context = _domain_render_context(ctx, modality, interface)
    # Canonical accuracy and benchmark commands are framework-owned. Hiding
    # them from the judge prevents duplicate official runs while preserving
    # domain-specific static review and targeted diagnostic guidance.
    judge_domain_context["accuracy_command"] = None
    judge_domain_context["benchmark_command"] = None
    domain_judge = render_domain_section(
        domain_definition,
        DomainRole.JUDGE,
        **judge_domain_context,
    )
    system_prompt = render_template(
        "judge_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        accuracy_command=ctx.judge_accuracy_command,
        benchmark_command=ctx.judge_benchmark_command,
        pass_criteria=plan.pass_criteria,
        modality=modality,
        interface=interface,
        domain_judge=domain_judge,
        retry=retry,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        profile_execution=ctx.run_environment_view.profile_execution,
        objective=objective,
        objective_location=ctx.objective_location,
        plan_artifact_location=plan_artifact_location,
        implementer_artifact_location=implementer_artifact_location,
        hypothesis_id=plan.hypothesis_id,
        hypothesis=plan.hypothesis,
        activation_evidence=plan.activation_evidence,
        falsification_criteria=plan.falsification_criteria,
        expected_effect=plan.expected_effect,
        minimum_acceptance_criteria=plan.minimum_acceptance_criteria,
        invariants=plan.invariants,
        implementer_outcome=implementation.hypothesis_outcome.value,
        implementer_evidence=implementation.evidence,
        implementer_perf_metric=implementation.perf_metric,
        implementer_perf_unit=implementation.perf_unit,
        implementer_metrics=implementation.metrics,
        implementer_evaluation_artifact=implementation.evaluation_artifact,
        candidate_disposition=implementation.candidate_disposition.value,
        candidate_metrics=implementation.candidate_metrics,
        candidate_evaluation_artifact=implementation.candidate_evaluation_artifact,
        candidate_operating_point=implementation.candidate_operating_point,
        candidate_retention_reason=implementation.candidate_retention_reason,
        gate_revalidation_pending=gate_revalidation_pending,
        gate_approved_perf_metric=gate_approved_perf_metric,
        gate_approved_perf_unit=gate_approved_perf_unit,
        gate_approved_metrics=gate_approved_metrics or {},
        gate_approved_evaluation_artifact=gate_approved_evaluation_artifact,
        progress_location=progress_location,
        pareto_archive_location=pareto_archive_location,
        validation_location=validation_location,
        validation_recipe_contract_location=validation_recipe_contract_location,
        framework_revert_applied=framework_revert_applied,
        framework_revert_round=framework_revert_round,
        framework_revert_commit=framework_revert_commit,
        framework_benchmark_enabled=framework_benchmark_enabled,
        official_evaluation_due=official_evaluation_due,
        official_evaluation_reason=official_evaluation_reason,
        pareto_archive_conflict=pareto_archive_conflict,
    )
    response = _invoke_read_only_role(
        ctx,
        agent=agent,
        role="judge",
        checkpoint_label=f"round-{round_number}-retry-{retry}-judge-input",
        kind="judge",
        system_prompt=system_prompt,
        user_prompt=(
            "Review the implementation per the criteria above. Return only the JSON verdict."
        ),
        response_cls=JudgeResponse,
        fallback_factory=lambda: JudgeResponse(
            analysis="Judge produced no structured response.",
            feedback="No structured response received.",
            verdict=Verdict.FAIL,
        ),
        round_label=f"round-{round_number}-retry-{retry}-judge",
        reuse_session=False,
    )
    response.skills_used, _ = _validate_skill_selections(ctx, response.skills_used)
    if response.verdict is Verdict.PASS and pareto_archive_conflict:
        response = response.model_copy(
            update={
                "analysis": (
                    f"{response.analysis}\n\nFramework Pareto guard: {pareto_archive_conflict}"
                ),
                "feedback": pareto_archive_conflict,
                "verdict": Verdict.FAIL,
            }
        )
    ctx.events.emit(
        CoreEventType.JUDGE_RESULT,
        status=(EventStatus.COMPLETED if response.verdict == Verdict.PASS else EventStatus.FAILED),
        round_label=f"round-{round_number}-retry-{retry}",
        agent_kind="judge",
        data=JudgeResultData(
            verdict=response.verdict.value,
            feedback=response.feedback,
            attempt=retry,
        ),
    )
    issue_board.append_judge(progress_path, round_number, retry, response)
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-judge")
    return response


def _run_single_agent_round(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    agent: SharedAgentHandle,
    round_number: int,
    retry: int,
    plan: OrchestratorPlan,
    modality: str | None,
    interface: str,
    domain_definition: DomainDefinition,
    feedback: str | None,
    progress_path: Path,
    progress_location: str,
    pareto_archive_location: str,
    objective: str,
    profile_focus: str,
    official_evaluation_due: bool = False,
    official_evaluation_reason: str | None = None,
    framework_benchmark_enabled: bool = False,
    pareto_records: list[RoundRecord] | None = None,
    space: MetricSpace,
) -> SingleAgentRoundResponse:
    """Invoke one agent that plays implementer + judge + profiler.

    Used when ``--inner-loop=single-agent``. The same backend that the
    multi-agent loop hands to the implementer is used here — it has
    workspace write access plus shell access for benchmarks/profiling.
    """
    plan.recommended_skills, resolved_skills = _validate_skill_selections(
        ctx, plan.recommended_skills
    )
    plan_artifact = issue_board.write_plan_artifact(progress_path, round_number, plan)
    plan_artifact_location = issue_board.display_path(plan_artifact, ctx.workspace)
    validation_location = issue_board.display_path(
        issue_board.validation_artifact_root(progress_path), ctx.workspace
    )
    domain_single_agent = render_domain_section(
        domain_definition,
        DomainRole.SINGLE_AGENT,
        **_domain_render_context(ctx, modality, interface),
    )
    domain_profiler = render_domain_section(
        domain_definition,
        DomainRole.PROFILER,
        **_domain_render_context(ctx, modality, interface),
    )
    effective_profiler = (
        _effective_profiler_definition(
            ctx.profiler_kind,
            supports_torch_profiler=domain_definition.supports_torch_profiler,
        )
        if ctx.profiler_kind is not ProfilerKind.NONE
        else None
    )
    system_prompt = render_template(
        "single_agent_round_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        reference_path=ctx.ref_name,
        modality=modality,
        interface=interface,
        domain_single_agent=domain_single_agent,
        domain_profiler=domain_profiler,
        task=plan.task,
        pass_criteria=plan.pass_criteria,
        hypothesis_id=plan.hypothesis_id,
        hypothesis=plan.hypothesis,
        activation_evidence=plan.activation_evidence,
        falsification_criteria=plan.falsification_criteria,
        expected_effect=plan.expected_effect,
        minimum_acceptance_criteria=plan.minimum_acceptance_criteria,
        invariants=plan.invariants,
        progress_location=progress_location,
        pareto_archive_location=pareto_archive_location,
        validation_location=validation_location,
        retry=retry,
        feedback=feedback,
        objective=objective,
        objective_location=ctx.objective_location,
        plan_artifact_location=plan_artifact_location,
        recommended_skills=resolved_skills,
        profile_focus=profile_focus,
        profiler_kind=ctx.profiler_kind,
        profiler_support_name=(effective_profiler.support_name if effective_profiler else None),
        profiler_mcp_name=(effective_profiler.mcp_name if effective_profiler else None),
        supports_torch_profiler=domain_definition.supports_torch_profiler,
        benchmark_command=ctx.judge_benchmark_command,
        accuracy_command=ctx.judge_accuracy_command,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        profile_execution=ctx.run_environment_view.profile_execution,
        official_evaluation_due=official_evaluation_due,
        official_evaluation_reason=official_evaluation_reason,
        framework_benchmark_enabled=framework_benchmark_enabled,
    )
    response = agent.turn_structured(
        (
            "Carry out the orchestrator's task above end-to-end "
            "(implement, self-judge, profile) and return only the JSON object."
        ),
        system_prompt=system_prompt,
        response_cls=SingleAgentRoundResponse,
        fallback_factory=lambda: SingleAgentRoundResponse(
            summary="Single-agent produced no structured response.",
            expected_behavior="unknown",
            self_review="No structured response received.",
            feedback="No structured response received.",
            verdict=Verdict.FAIL,
            bottlenecks="",
            suggestions="",
            profile_analysis="",
        ),
        label=f"round-{round_number}-retry-{retry}-single-agent",
        reuse_session=True,
        session_key=AgentSessionKey(SessionScope.HYPOTHESIS, plan.hypothesis_id),
    )
    response.skill_context_updates, _ = _validate_skill_selections(
        ctx, response.skill_context_updates
    )
    if response.skill_context_updates:
        plan.recommended_skills, _ = _validate_skill_selections(
            ctx, [*plan.recommended_skills, *response.skill_context_updates]
        )
        issue_board.write_plan_artifact(progress_path, round_number, plan)
    archive_conflict = _pareto_archive_conflict(
        candidate_disposition=response.candidate_disposition,
        candidate_metrics=dict(response.candidate_metrics),
        records=pareto_records or [],
        space=space,
    )
    if response.verdict is Verdict.PASS and archive_conflict:
        response = response.model_copy(
            update={
                "self_review": (
                    f"{response.self_review}\n\nFramework Pareto guard: {archive_conflict}"
                ),
                "feedback": archive_conflict,
                "verdict": Verdict.FAIL,
            }
        )
    issue_board.append_single_agent_round(progress_path, round_number, retry, response)
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-single-agent")
    return response


def _profiler_summary_from_single_agent(
    response: SingleAgentRoundResponse,
) -> ProfilerSummary:
    """Adapt a single-agent response into a ProfilerSummary for the orchestrator."""
    return ProfilerSummary(
        analysis=response.profile_analysis,
        bottlenecks=response.bottlenecks,
        suggestions=response.suggestions,
        perf_metric=response.perf_metric,
        perf_unit=response.perf_unit,
    )


def _validation_input_digest(workspace: Path, recipe: ValidationRecipe) -> str:
    """Hash the declared workspace inputs that determine recipe reuse."""
    digest = hashlib.sha256()
    workspace_root = workspace.resolve()
    total_files = 0
    total_bytes = 0
    for relative in sorted(recipe.input_paths):
        unresolved = workspace / relative
        if unresolved.is_symlink():
            raise ValueError(f"validation input must not be a symlink: {relative}")  # noqa: TRY003  # tracked: #288
        path = unresolved.resolve()
        if not path.is_relative_to(workspace_root):
            raise ValueError(f"validation input escapes workspace: {relative}")  # noqa: TRY003  # tracked: #288
        if not path.exists():
            raise ValueError(f"validation input does not exist: {relative}")  # noqa: TRY003  # tracked: #288
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0dir\0" if path.is_dir() else b"\0file\0")
        entries = [path]
        if path.is_dir():
            entries = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
        for entry in entries:
            if entry.is_symlink():
                raise ValueError(f"validation input must not be a symlink: {relative}")  # noqa: TRY003  # tracked: #288
            total_files += 1
            total_bytes += entry.stat().st_size
            if total_files > 4096 or total_bytes > 256 * 1024 * 1024:  # noqa: PLR2004  # tracked: #288
                raise ValueError("validation inputs exceed the 4096-file/256-MiB reuse-hash limit")  # noqa: TRY003  # tracked: #288
            entry_relative = entry.relative_to(workspace_root).as_posix()
            digest.update(entry_relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(entry.read_bytes())
            digest.update(b"\0")
    digest.update(recipe.command.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(recipe.timeout_seconds).encode("ascii"))
    return digest.hexdigest()


def _reusable_validation_result(
    progress_path: Path,
    recipe: ValidationRecipe,
    input_digest: str,
) -> FrameworkValidationResult | None:
    """Return the newest matching framework PASS, if one exists."""
    for artifact in reversed(issue_board.validation_result_artifact_paths(progress_path)):
        try:
            payload = json.loads(artifact.read_text())
            results = payload.get("results", [])
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        for raw in reversed(results):
            try:
                result = FrameworkValidationResult.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if result.passed and result.input_digest == input_digest and result.recipe == recipe:
                return result.model_copy(update={"reused": True})
    return None


def _load_validation_recipes(workspace: Path, artifact: str) -> list[ValidationRecipe]:
    """Load and validate a candidate-authored recipe file inside the workspace."""
    workspace_root = workspace.resolve()
    path = (workspace / artifact).resolve()
    if not path.is_relative_to(workspace_root):
        raise ValueError("validation recipe artifact escapes the workspace")  # noqa: TRY003  # tracked: #288
    if not path.is_file():
        raise ValueError(f"validation recipe artifact does not exist: {artifact}")  # noqa: TRY003  # tracked: #288
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"validation recipe artifact is not valid JSON: {exc}") from exc  # noqa: TRY003  # tracked: #288
    try:
        return ValidationRecipeArtifact.model_validate(payload).recipes
    except (TypeError, ValueError) as exc:
        raise ValueError(f"validation recipe artifact does not match version 1: {exc}") from exc  # noqa: TRY003  # tracked: #288
