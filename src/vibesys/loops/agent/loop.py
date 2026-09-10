"""Orchestrator-driven build loop.

An Orchestrator agent decides each round what the Implementer should build and
what pass criteria the Judge should enforce, optionally asking a Profiler to
collect kernel-level data first.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from collections.abc import Sequence  # noqa: TC003  # tracked: #288
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import Any, Literal

from vibesys.agents.base import ResponseFallback
from vibesys.agents.factory import resolve_agent_driver
from vibesys.agents.progress import RoundProgress
from vibesys.agents.session_key import AgentSessionKey, SessionScope
from vibesys.config import Config, as_config
from vibesys.constants import DEFAULT_AGENT_BACKEND, DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.context import create_run_context
from vibesys.domains.base import DomainDefinition, DomainName, DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.input_manifest import BenchmarkResult, WorkspaceSource  # noqa: TC001  # tracked: #288
from vibesys.loops.agent import issue_board
from vibesys.loops.agent.attempt import (
    JudgeOutcome,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    attempt_was_reviewed,
    recorded_judge_verdict,
)
from vibesys.loops.agent.hypotheses import (
    ResolutionEvidence,
    adopt_metric_space,
    append_round,
    apply_strategy_updates,
    metric_baseline,
    record_metric_value,
    resolve_hypothesis_outcome,
    scalar_candidate_retained,
    start_hypothesis,
    trusted_perf_provenance,
    update_active_hypothesis,
)
from vibesys.loops.agent.model import (
    AgentRunState,
    Hypothesis,
    HypothesisResolution,
)
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.loops.gates import (
    GATE_RECORD_TAIL_CHARS,
    BenchmarkContract,
    FrameworkBenchmarkOutcome,
    framework_command_timeout,
    run_accuracy_gate,
    run_benchmark_gate,
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
from vibesys.run import LoopContext, RepositoryVisibility, RunIntegration, RunStateNamespace
from vibesys.run.events import (
    BenchmarkResultData,
    CoreEventType,
    EventStatus,
    ExperimentsChangedData,
    JudgeResultData,
    RoundFinishedData,
)
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
    run_environment_record,
)
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
from vs_loop_state.agent import PerfProvenance, RoundHistory, RoundRecord
from vs_project import AgentRunConfiguration

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


def _persist_agent_run_state(
    ctx: LoopContext,
    store: AgentRunStateStore,
    state: AgentRunState,
    *,
    label: str,
) -> None:
    """Persist only authoritative agent state without staging candidate edits."""
    store.save(state)
    ctx.state.commit(label, store.namespace)


def _persist_active_hypothesis(
    ctx: LoopContext,
    store: AgentRunStateStore,
    state: AgentRunState,
    hypothesis: Hypothesis,
    *,
    label: str,
) -> AgentRunState:
    """Replace and persist the active hypothesis inside its owning aggregate."""
    updated = update_active_hypothesis(state, hypothesis)
    _persist_agent_run_state(ctx, store, updated, label=label)
    return updated


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
    if not objectives:
        return (
            "No objective axes are configured. Use objectives.toml to enable "
            "multi-objective checkpoint retention; official scalar tracking remains active."
        )

    lines = [
        "Configured axes: "
        + ", ".join(f"{objective.name}:{objective.direction}" for objective in objectives),
        (
            "Dominance is variance-aware: a point removes another only when it is no worse "
            f"within {space.relative_noise:.0%} on every axis and better by more than "
            f"{space.relative_noise:.0%} on at least one."
        ),
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


def _invoke_read_only_role(
    ctx: LoopContext,
    *,
    role: str,
    checkpoint_label: str,
    allowed_workspace_paths: tuple[str, ...] = (),
    **invoke_kwargs: Any,  # noqa: ANN401  # tracked: #288
) -> Any:  # noqa: ANN401  # tracked: #288
    """Invoke an evidence-reading role and undo unauthorized mutations.

    Prompt-level role boundaries are useful guidance, but they are not an
    enforcement mechanism. Commit the framework's current state before the
    turn, then restore that exact tree if the agent writes tracked or untracked
    files outside its narrow allowlist. The structured response remains usable
    after restoration. Allowlisted text files are preserved across a full-tree
    restore so one permitted write cannot smuggle unrelated candidate edits.
    """
    ctx.snapshot_workspace(checkpoint_label)
    checkpoint = ctx.git.current_sha()
    if checkpoint is None:
        raise RuntimeError(f"Cannot isolate {role}: workspace checkpoint is unavailable")  # noqa: TRY003  # tracked: #288

    try:
        return ctx.invoke(**invoke_kwargs)
    finally:

        def is_allowed(path: str) -> bool:
            return any(
                path == allowed.rstrip("/") or path.startswith(f"{allowed.rstrip('/')}/")
                for allowed in allowed_workspace_paths
            )

        changes = ctx.git.pending_changes()
        unauthorized = [path for path in changes if not is_allowed(path)]
        if unauthorized:
            checkout_kwargs: dict[str, Any] = {"clean": True}
            if allowed_workspace_paths:
                checkout_kwargs["preserve_paths"] = allowed_workspace_paths
            if not ctx.git.checkout_tree(checkpoint, **checkout_kwargs):
                raise RuntimeError(  # noqa: TRY003  # tracked: #288
                    f"Cannot isolate {role}: failed to restore workspace checkpoint "
                    f"{checkpoint[:12]}"
                )
            remaining = [path for path in ctx.git.pending_changes() if not is_allowed(path)]
            if remaining:
                raise RuntimeError(  # noqa: TRY003  # tracked: #288
                    f"Cannot isolate {role}: workspace is still modified after restore: "
                    f"{', '.join(remaining[:8])}"
                )
            shown = ", ".join(unauthorized[:8])
            suffix = "" if len(unauthorized) <= 8 else f", ... (+{len(unauthorized) - 8} more)"  # noqa: PLR2004  # tracked: #288
            ctx.lprint(
                f"[role-isolation] reverted {len(unauthorized)} workspace change(s) "
                f"attempted by {role}: {shown}{suffix}"
            )


def _is_fresh_cold_start(round_number: int, records: list[RoundRecord]) -> bool:
    """True for round 1 of a fresh run (no prior rounds recorded)."""
    return round_number == 1 and not records


def _run_pre_round_decision(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
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
        ctx.lprint(f"[warn] profiler failed: {exc}")
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
        ctx.lprint("[skills] ignored recommendations because no skills are installed")
        return [], []
    try:
        catalog = build_skill_catalog(skill_sources)
        resolved, diagnostics = resolve_skill_selections(selections, catalog)
    except (OSError, ValueError) as exc:
        ctx.lprint(
            f"[skills] ignored recommendations because the catalog is invalid: "
            f"{type(exc).__name__}: {exc}"
        )
        return [], []
    for diagnostic in diagnostics:
        ctx.lprint(f"[skills] {diagnostic}")

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
    )
    # Make this attempt number durable before the turn starts. A process killed
    # mid-invoke writes no completed artifact, so a resume that counted only
    # those would reuse this attempt number and replay the round label below
    # over paid work.
    issue_board.write_implementer_start_marker(progress_path, round_number, retry)
    fallback = ResponseFallback(_missing_implementer_response)
    timed_out = False
    try:
        response = ctx.invoke(
            kind="implementer",
            system_prompt=system_prompt,
            user_prompt=(
                "Execute the required continuation step for the active hypothesis; "
                "do not merely restate prior work. Return only the JSON object."
                if continuation_step
                else "Work persistently on the active hypothesis and return only the JSON object."
            ),
            response_cls=ImplementerResponse,
            fallback_factory=fallback,
            round_label=f"round-{round_number}-retry-{retry}-implementer",
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
    response = ctx.invoke(
        kind="implementer",
        system_prompt=system_prompt,
        user_prompt=(
            "Carry out the orchestrator's task above end-to-end "
            "(implement, self-judge, profile) and return only the JSON object."
        ),
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
        round_label=f"round-{round_number}-retry-{retry}-single-agent",
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


def _run_framework_validation_gate(  # noqa: C901, PLR0912, PLR0915  # tracked: #288
    ctx: LoopContext,
    *,
    recipe_artifact: str | None,
    round_number: int,
    retry: int,
    progress_path: Path,
) -> str | None:
    """Execute judge-audited local checks once and cache exact-input passes.

    This gate intentionally excludes target, deployment, profiler, benchmark,
    and official evaluator work. The Judge audits that boundary before a PASS
    can reach this function. Commands must be non-mutating; any workspace write
    fails the gate and is restored to the pre-validation checkpoint.
    """
    if recipe_artifact is None:
        return None

    try:
        recipes = _load_validation_recipes(ctx.workspace, recipe_artifact)
    except ValueError as exc:
        return f"Framework local validation recipe error: {exc}."

    labels = [recipe.name for recipe in recipes]
    if len(set(labels)) != len(labels):
        return "Framework local validation recipes contain duplicate names."

    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-validation-input")
    checkpoint = ctx.git.current_sha()
    if checkpoint is None:
        return "Framework local validation could not establish a workspace checkpoint."

    results: list[FrameworkValidationResult] = []
    restore_required = False
    for recipe in recipes:
        try:
            input_digest = _validation_input_digest(ctx.workspace, recipe)
        except (OSError, ValueError) as exc:
            results.append(
                FrameworkValidationResult(
                    recipe=recipe,
                    input_digest="",
                    passed=False,
                    error=str(exc),
                )
            )
            break

        reused = _reusable_validation_result(progress_path, recipe, input_digest)
        if reused is not None:
            results.append(reused)
            ctx.lprint(f"[framework-validation] reused PASS: {recipe.name}")
            continue

        ctx.lprint(f"[framework-validation] running {recipe.name}: {recipe.command}")
        try:
            execution = ctx.judge_backend.execute(
                recipe.command,
                timeout=recipe.timeout_seconds,
            )
            output = execution.output.strip()
            passed = execution.exit_code == 0
            result = FrameworkValidationResult(
                recipe=recipe,
                input_digest=input_digest,
                passed=passed,
                exit_code=execution.exit_code,
                output=output[-GATE_RECORD_TAIL_CHARS:],
                error=None if passed else "command exited nonzero",
            )
        except Exception as exc:  # noqa: BLE001  # tracked: #288
            result = FrameworkValidationResult(
                recipe=recipe,
                input_digest=input_digest,
                passed=False,
                error=f"command could not be executed: {exc}",
            )

        changes = ctx.git.pending_changes()
        if changes:
            restore_required = True
            shown = ", ".join(changes[:8])
            suffix = "" if len(changes) <= 8 else f", ... (+{len(changes) - 8} more)"  # noqa: PLR2004  # tracked: #288
            result = result.model_copy(
                update={
                    "passed": False,
                    "error": (f"validation command mutated the workspace: {shown}{suffix}"),
                }
            )
        results.append(result)
        if not result.passed:
            break

    if restore_required:  # noqa: SIM102  # tracked: #288
        if not ctx.git.checkout_tree(checkpoint, clean=True):
            results[-1] = results[-1].model_copy(
                update={
                    "passed": False,
                    "error": "validation mutation could not be restored",
                }
            )

    artifact = issue_board.write_validation_result_artifact(
        progress_path,
        round_number,
        retry,
        results,
    )
    artifact_location = issue_board.display_path(artifact, ctx.workspace)
    issue_board.append_framework_validation_gate(
        progress_path,
        round_number,
        retry,
        artifact=artifact_location,
        results=results,
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-validation")

    failed = next((result for result in results if not result.passed), None)
    if failed is None:
        ctx.lprint("[framework-validation] PASS")
        return None
    detail = failed.error or failed.output or "unknown failure"
    ctx.lprint(f"[framework-validation] FAIL: {failed.recipe.name}: {detail}")
    return (
        f"Framework local validation failed for {failed.recipe.name!r}: {detail}. "
        f"Inspect `{artifact_location}` and repair only the affected local contract."
    )


def _deployment_release_env_var(ctx: LoopContext) -> str | None:
    return ctx.run_environment_view.deployment_release_env_var


def _with_candidate_revision(
    command: str,
    candidate_revision: str | None,
    *,
    release_deployment_env_var: str | None = None,
) -> str:
    """Annotate an official command with its bounded deployment-lease lifecycle."""
    environment: list[str] = []
    if candidate_revision:
        environment.append(f"VIBESYS_CANDIDATE_REVISION={shlex.quote(candidate_revision)}")
    if release_deployment_env_var:
        environment.append(f"{release_deployment_env_var}=1")
    if not environment:
        return command
    return f"env {' '.join(environment)} {command}"


def _run_framework_accuracy_gate(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    round_number: int,
    retry: int,
    progress_path: Path,
    timeout_seconds: int | None = None,
    candidate_revision: str | None = None,
    release_deployment_after: bool = False,
) -> str | None:
    """Run the immutable manifest accuracy command after an agent reports PASS."""
    command = ctx.judge_accuracy_command
    execution_command = None
    if command:
        execution_command = _with_candidate_revision(
            command,
            candidate_revision,
            release_deployment_env_var=(
                _deployment_release_env_var(ctx) if release_deployment_after else None
            ),
        )
    result = run_accuracy_gate(
        ctx,
        process_id=f"accuracy-{round_number}-{retry}",
        timeout_seconds=framework_command_timeout(ctx, timeout_seconds),
        execution_command=execution_command,
    )
    if result.passed and not result.executed:
        return None

    issue_board.append_framework_accuracy_gate(
        progress_path,
        round_number,
        retry,
        command=result.command or "(not configured)",
        passed=result.passed,
        output=result.output[-GATE_RECORD_TAIL_CHARS:],
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-accuracy")
    return result.feedback


def _run_framework_benchmark(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    result_spec: BenchmarkResult | None,
    result_protocol: Literal[2] | None = None,
    objectives: Sequence[Objective] = (),
    round_number: int,
    retry: int,
    progress_path: Path,
    timeout_seconds: int | None = None,
    candidate_revision: str | None = None,
) -> FrameworkBenchmarkOutcome:
    """Run the shared benchmark gate and record its agent-loop bookkeeping.

    The gate itself (result recovery, parsing, and the collision-proof result
    path) lives in :mod:`vibesys.loops.gates`; this wrapper owns what is
    agent-loop specific: progress notes, workspace snapshots, and the
    supervisor's benchmark-result event.
    """
    execution_base = None
    if ctx.judge_benchmark_command:
        execution_base = _with_candidate_revision(
            ctx.judge_benchmark_command,
            candidate_revision,
            release_deployment_env_var=_deployment_release_env_var(ctx),
        )
    result = run_benchmark_gate(
        ctx,
        result_spec=result_spec,
        result_protocol=result_protocol,
        objectives=objectives,
        process_id=f"benchmark-{round_number}-{retry}",
        output_slug=f"{round_number}-{retry}",
        timeout_seconds=framework_command_timeout(ctx, timeout_seconds),
        execution_base=execution_base,
    )
    if not result.executed:
        return result.outcome

    issue_board.append_framework_benchmark(
        progress_path,
        round_number,
        retry,
        command=result.command or "(not configured)",
        passed=result.passed,
        metric_name=(
            result.outcome.metric_name or (result_spec.metric if result_spec is not None else None)
        ),
        metric_value=result.outcome.metric_value,
        output=result.output[-GATE_RECORD_TAIL_CHARS:],
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-benchmark")
    if (
        result.passed
        and result.outcome.metric_name is not None
        and result.outcome.metric_value is not None
    ):
        ctx.events.emit(
            CoreEventType.BENCHMARK_RESULT,
            status=EventStatus.COMPLETED,
            round_label=f"round-{round_number}",
            data=BenchmarkResultData(
                metric=result.outcome.metric_name,
                value=result.outcome.metric_value,
                unit=result.outcome.metric_unit or result.outcome.metric_name,
            ),
        )
    return result.outcome


def _reconcile_model_requests(ctx: LoopContext) -> str | None:
    """Stage any candidate-declared model weights before the framework gates.

    The candidate may declare extra model weights it needs in
    ``.vibesys/models.json`` (see ``vibesys.sandbox.model_requests``). This runs
    once per gate invocation, before deploy; a malformed or disallowed manifest
    is returned as gate feedback so the candidate can correct it rather than
    crashing the run. Only meaningful for Modal runs (weights live in Modal
    Volumes); a no-op otherwise.
    """
    if getattr(ctx.run_environment_view, "env_kind", "local") != "modal":
        return None
    from vibesys.sandbox.model_requests import (  # noqa: PLC0415  # tracked: #288
        ModelRequestError,
        reconcile_model_requests,
    )

    try:
        volumes = reconcile_model_requests(ctx.workspace, log=ctx.lprint)
    except ModelRequestError as exc:
        ctx.lprint(f"[model-request] rejected: {exc}")
        return f"Model-weight request could not be satisfied: {exc}"
    if volumes:
        ctx.lprint(f"[model-request] staged {len(volumes)} model volume(s): " + ", ".join(volumes))
    return None


def _run_framework_gates(  # noqa: PLR0913  # tracked: #288
    ctx: LoopContext,
    *,
    benchmark_result: BenchmarkResult | None,
    benchmark_result_protocol: Literal[2] | None = None,
    objectives: Sequence[Objective] = (),
    round_number: int,
    retry: int,
    progress_path: Path,
    accuracy_timeout_seconds: int | None = None,
    benchmark_timeout_seconds: int | None = None,
    reuse_accuracy_pass: bool = False,
    candidate_revision: str | None = None,
) -> tuple[str | None, FrameworkBenchmarkOutcome, bool]:
    """Run the framework-owned gates, returning the first failure's feedback.

    The benchmark outcome is always returned so a passing protocol-path run can
    carry its complete metric row to the round record; it is empty whenever the
    benchmark did not run.
    """
    if ctx.agent_client.backend_name == "stub":
        return None, FrameworkBenchmarkOutcome(), False
    resource_feedback = _reconcile_model_requests(ctx)
    if resource_feedback is not None:
        return resource_feedback, FrameworkBenchmarkOutcome(), False
    if reuse_accuracy_pass:
        feedback = None
        issue_board.append_framework_accuracy_gate(
            progress_path,
            round_number,
            retry,
            command=ctx.judge_accuracy_command or "(not configured)",
            passed=True,
            output=(
                "Reused the prior framework-owned PASS for this exact candidate "
                "commit; a later gate, not accuracy, caused the retry."
            ),
        )
        ctx.lprint("[framework-accuracy] reused prior PASS for unchanged candidate")
    else:
        feedback = _run_framework_accuracy_gate(
            ctx,
            round_number=round_number,
            retry=retry,
            progress_path=progress_path,
            timeout_seconds=accuracy_timeout_seconds,
            candidate_revision=candidate_revision,
            release_deployment_after=(
                (benchmark_result is None and benchmark_result_protocol is None)
                or not ctx.judge_benchmark_command
            ),
        )
    if feedback is not None:
        return feedback, FrameworkBenchmarkOutcome(), False
    benchmark = _run_framework_benchmark(
        ctx,
        result_spec=benchmark_result,
        result_protocol=benchmark_result_protocol,
        objectives=objectives,
        round_number=round_number,
        retry=retry,
        progress_path=progress_path,
        timeout_seconds=benchmark_timeout_seconds,
        candidate_revision=candidate_revision,
    )
    return benchmark.feedback, benchmark, True


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_agent_loop(  # noqa: C901, PLR0912, PLR0913, PLR0915  # tracked: #288
    config: Config,
    exp_name: str,
    input_path: str,
    accuracy_command: str,
    benchmark_command: str,
    objective: str,
    *,
    runs_dir: Path | None,
    task_name: str | None = None,
    task_root: Path | None = None,
    metrics: MetricSpace,
    workspace_sources: tuple[WorkspaceSource, ...] = (),
    evaluator_path: Path | None = None,
    evaluator_package_root: Path | None = None,
    benchmark_result: BenchmarkResult | None = None,
    benchmark_result_protocol: Literal[2] | None = None,
    accuracy_timeout_seconds: int | None = None,
    benchmark_timeout_seconds: int | None = None,
    max_rounds: int = 24,
    max_retries_per_round: int = 3,
    judge_every: int = 3,
    official_eval_every: int = 3,
    memory_layout: str = "files",
    start_round: int | None = 1,
    existing: bool = False,
    operator_constraints: tuple[str, ...] = (),
    trusted_input_baseline: str | None = None,
    debug: bool = False,
    profiler_kind: ProfilerKind = ProfilerKind.AUTO,
    skills_dirs: list[str] | None = None,
    run_environment: RunEnvironmentSpec | None = None,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
    modality: str | None = None,
    inner_loop: str = "multi-agent",
    domain: DomainName | None = None,
    interface: str = DEFAULT_INTERFACE,
    remote_repo: str | None = None,
    repo_visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE,
    integration: RunIntegration | None = None,
) -> bool:
    """Run the orchestrator-driven build loop.

    Returns True iff the orchestrator declared the objective met within
    ``max_rounds``.  Returns False when the round budget is exhausted.

    ``inner_loop`` selects how each round's implement/judge/profile work
    is dispatched:

    - ``"multi-agent"`` (default): three specialist agents — implementer,
      judge, profiler — invoked in sequence.
    - ``"single-agent"``: one agent does all three in a single
      invocation per retry. Pre-round decision and standalone profiler
      passes are skipped; the prior round's profile output is fed to the
      orchestrator as ``profiler_summary``.

    ``interface`` selects only the evaluator-to-candidate process boundary:

    - ``"inprocess"`` (default): evaluator-owned code invokes the candidate
      directly using the input-defined contract.
    - ``"service"``: evaluator-owned code communicates with a running service
      through its network interface.

    Language, tooling, and artifact requirements come from the domain and input
    bundle rather than the process-boundary mode.
    """
    if inner_loop not in _INNER_LOOPS:
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"Unknown inner_loop {inner_loop!r}; choose from {', '.join(_INNER_LOOPS)}"
        )
    if max_retries_per_round < 1:
        # Guard against a zero-iteration retry loop: with no attempts the
        # round bookkeeping below would reference an unbound loop variable.
        raise ValueError(f"max_retries_per_round must be >= 1, got {max_retries_per_round}")  # noqa: TRY003  # tracked: #288
    if judge_every < 1:
        raise ValueError(f"judge_every must be >= 1, got {judge_every}")  # noqa: TRY003  # tracked: #288
    if official_eval_every < 1:
        raise ValueError(f"official_eval_every must be >= 1, got {official_eval_every}")  # noqa: TRY003  # tracked: #288
    if memory_layout not in issue_board.MEMORY_LAYOUTS:
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"Unknown memory_layout {memory_layout!r}; "
            f"choose from {', '.join(issue_board.MEMORY_LAYOUTS)}"
        )
    if interface not in _INTERFACES:
        raise ValueError(f"Unknown interface {interface!r}; choose from {', '.join(_INTERFACES)}")  # noqa: TRY003  # tracked: #288
    if domain is None:
        raise ValueError("domain is required; declare [agent].domain in vibesys.input.toml")  # noqa: TRY003  # tracked: #288
    # Resolve the registered domain once (fail fast on an unknown name). The
    # per-role files carry language, tooling, and use-case-specific contracts.
    domain_definition = resolve_domain(domain)
    objectives = list(metrics.objectives)
    # Either result contract means a framework-owned benchmark runs this round,
    # which is what the prompts and the official-evaluation record key off.
    framework_benchmark_configured = benchmark_result is not None or (
        benchmark_result_protocol is not None
    )
    # Axes recorded in the run manifest. The server reads them back to build a
    # metric space for runs whose unified state predates one; they include the
    # framework benchmark's own axis, which is not a frontier axis.
    manifest_axes: dict[str, Literal["max", "min"]] = {
        objective.name: objective.direction for objective in objectives
    }
    if benchmark_result is not None:
        # The legacy scalar result contract predates explicit directions and
        # has always defined its reported metric as a maximization objective.
        manifest_axes.setdefault(benchmark_result.metric, "max")
    if modality is None and domain_definition.name is DomainName.LLM_SERVING:
        modality = "text_generation"
    run_environment = run_environment or make_run_environment_spec()
    normalized_config = as_config(config)
    resolved_agent_backend = (
        "stub"
        if agent_backend == "stub"
        else agent_backend or normalized_config.agent.backend or DEFAULT_AGENT_BACKEND
    )
    project_configuration = AgentRunConfiguration(
        outer_loop="agent",
        run_environment=run_environment_record(run_environment),
        inner_loop=inner_loop,
        interface=interface,
        model=normalized_config.model.name,
        agent_backend=resolved_agent_backend,
        agent_driver=(
            resolve_agent_driver(normalized_config) if resolved_agent_backend == "cli" else None
        ),
        cli_provider=(
            cli_provider or normalized_config.agent.cli_provider or "codex"
            if agent_backend != "stub"
            else None
        ),
        compute_backend=backend.value,
        profiler=profiler_kind.value,
        max_rounds=max_rounds,
        max_retries_per_round=max_retries_per_round,
        judge_every=judge_every,
        official_eval_every=official_eval_every,
        memory_layout=memory_layout,
        modality=modality,
        cli_timeout=normalized_config.agent.cli_timeout,
        default_reasoning_effort=normalized_config.thinking.level,
        outer_model=normalized_config.agent.outer.model,
        outer_reasoning_effort=normalized_config.agent.outer.reasoning_effort,
        inner_model=normalized_config.agent.inner.model,
        inner_reasoning_effort=normalized_config.agent.inner.reasoning_effort,
        operator_constraints=operator_constraints,
        objectives=tuple(f"{name}:{direction}" for name, direction in manifest_axes.items()),
    )
    ctx = create_run_context(
        config=normalized_config,
        exp_name=exp_name,
        runs_dir=runs_dir,
        input_path=input_path,
        accuracy_command=accuracy_command,
        benchmark_command=benchmark_command,
        task_name=task_name,
        task_root=task_root,
        workspace_sources=workspace_sources,
        evaluator_path=evaluator_path,
        evaluator_package_root=evaluator_package_root,
        benchmark_output_argument=BenchmarkContract(
            result_spec=benchmark_result,
            result_protocol=benchmark_result_protocol,
        ).output_argument,
        objective=objective,
        existing=existing,
        project_configuration=project_configuration,
        trusted_input_baseline=trusted_input_baseline,
        debug=debug,
        profiler_kind=profiler_kind,
        profiler_domain=domain_definition.name,
        skills_dirs=skills_dirs,
        run_environment=run_environment,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backend=backend,
        environment_hooks=domain_definition.environment_hooks,
        remote_repo=remote_repo,
        repo_visibility=repo_visibility,
        agent_state_model_type=AgentRunState,
        integration=integration,
    )
    ctx.lprint(f"[log] orchestrate run: {ctx.run_log_path}")
    ctx.lprint(f"[log] project root: {ctx.project_root}")
    ctx.lprint(f"[log] objective: {objective.splitlines()[0] if objective else '(empty)'}")

    roadmap_path, progress_path = issue_board.resolve_paths(ctx.workspace, memory_layout)
    issue_board.ensure_progress_file(progress_path)
    issue_board.ensure_roadmap_file(roadmap_path)
    issue_board.write_validation_recipe_schema(progress_path)
    progress_location = issue_board.display_path(progress_path, ctx.workspace)
    roadmap_location = issue_board.display_path(roadmap_path, ctx.workspace)
    pareto_archive_path = issue_board.pareto_archive_path(progress_path)
    pareto_archive_location = issue_board.display_path(pareto_archive_path, ctx.workspace)

    portable_agent_state = ctx.state.portable(RunStateNamespace.AGENT)
    local_agent_state = ctx.state.local(RunStateNamespace.AGENT)
    state_store = AgentRunStateStore(portable_agent_state)
    legacy_records = ctx.state.completed_rounds()
    agent_run_state = state_store.migrate_legacy(
        rounds=legacy_records,
        local_namespace=local_agent_state,
        legacy_space=metrics,
    )
    # The one write of the run's metric space. Everything downstream -- round
    # projection, retention, the Pareto frontier, ``--resume`` reprojection,
    # and the server read path -- takes it from the persisted state, so the
    # launching task file wins here and nowhere else re-reads it. Rounds that
    # already recorded a comparison keep it; only their derivation changes.
    agent_run_state = adopt_metric_space(agent_run_state, metrics)
    active_hypothesis = agent_run_state.active_hypothesis
    if active_hypothesis is not None and _backfill_revert_commit(
        active_hypothesis, agent_run_state.rounds
    ):
        agent_run_state = update_active_hypothesis(agent_run_state, active_hypothesis)

    # Replace the legacy portable namespace exactly, then remove the local
    # restart checkpoint only after its unified replacement is committed.
    state_store.save(agent_run_state)
    state_store.cleanup_legacy_portable([record.round_number for record in legacy_records])
    ctx.state.commit("agent: migrate unified hypothesis state", state_store.namespace)
    state_store.cleanup_legacy_local(local_agent_state)

    round_history = RoundHistory(records=agent_run_state.rounds)
    records = round_history.records

    carry = _CarryOver(regression_info=_terminal_workspace_notice(records))
    round_number = start_round if start_round is not None else len(records) + 1
    if round_number > max_rounds:
        raise ValueError(  # noqa: TRY003  # tracked: #288
            f"This run has completed {round_number - 1} rounds; max_rounds={max_rounds} "
            "is a total limit. Increase --max-rounds to continue."
        )

    # When inner_loop == "single-agent", we don't run a separate
    # pre-round decision or profiler invocation. We thread the previous
    # round's combined response into the orchestrator's next plan as a
    # synthesized ProfilerSummary, and remember its profile focus across
    # rounds. The first round has no prior profile to feed forward.
    last_single_agent_response: SingleAgentRoundResponse | None = None
    last_profile_focus: str = "general latency hotspots on /v1/completions"

    try:
        while round_number <= max_rounds:
            ctx.switch_log_file(f"round{round_number:03d}")
            issue_board.write_pareto_archive(
                progress_path,
                _pareto_archive_summary(records, agent_run_state.metrics),
            )
            round_progress = RoundProgress(round_number, max_rounds)
            ctx.lprint(f"\n{'=' * 60}\n  {round_progress.label()}\n{'=' * 60}\n")

            with ctx.progress(round_progress):
                # The designer runs only when selecting a new causal claim.
                # A continuing hypothesis remains owned by its persistent
                # implementer session without another designer intervention.
                profiler_summary: ProfilerSummary | None = None
                pre_decision: PreRoundDecision | None = None
                if active_hypothesis is None:
                    if inner_loop == "multi-agent":
                        # Round 1 used to skip this decision, which also skipped
                        # the profiler nested under it: the round holding the
                        # least evidence was the one round where nothing could
                        # ask for measurement. It now decides like every other
                        # round, told by ``has_history`` that it has no prior
                        # entry to read and must establish runnability itself.
                        pre_decision = _run_pre_round_decision(
                            ctx,
                            round_number=round_number,
                            objective=objective,
                            carry=carry,
                            progress_path=progress_path,
                            progress_location=progress_location,
                            has_history=not _is_fresh_cold_start(round_number, records),
                        )
                        if pre_decision.need_profile and ctx.profiler_kind is not ProfilerKind.NONE:
                            profiler_summary = _run_profiler(
                                ctx,
                                round_number=round_number,
                                profile_focus=pre_decision.profile_focus
                                or "general steady-state benchmark hotspots",
                                modality=modality,
                                interface=interface,
                                domain_definition=domain_definition,
                                progress_path=progress_path,
                                objective=objective,
                            )
                    elif last_single_agent_response is not None:
                        profiler_summary = _profiler_summary_from_single_agent(
                            last_single_agent_response
                        )

                    plateau_warning = _detect_plateau(records)
                    provisional_candidates = _provisional_candidates_since_official(records)
                    plan = _run_orchestrator_plan(
                        ctx,
                        agent_run_state=agent_run_state,
                        round_number=round_number,
                        objective=objective,
                        profiler_summary=profiler_summary,
                        carry=carry,
                        progress_path=progress_path,
                        progress_location=progress_location,
                        roadmap_location=roadmap_location,
                        pareto_archive_location=pareto_archive_location,
                        plateau_warning=plateau_warning,
                        modality=modality,
                        interface=interface,
                        domain_definition=domain_definition,
                        framework_benchmark_enabled=framework_benchmark_configured,
                        official_eval_every=official_eval_every,
                        provisional_candidates=provisional_candidates,
                        official_eval_cadence_due=(
                            provisional_candidates + 1 >= official_eval_every
                        ),
                    )
                    parent_round = (
                        plan.revert_to_round
                        if plan.revert_to_round is not None
                        else round_number - 1
                        if round_number > 1
                        else None
                    )
                    parent_record = next(
                        (
                            record
                            for record in reversed(records)
                            if record.round_number == parent_round
                        ),
                        None,
                    )
                    agent_run_state = start_hypothesis(
                        agent_run_state,
                        plan,
                        started_round=round_number,
                        parent_round=parent_round,
                        parent_commit=(
                            parent_record.commit
                            if parent_record is not None and parent_record.commit is not None
                            else ctx.git.current_sha()
                        ),
                    )
                    active_hypothesis = agent_run_state.active_hypothesis
                    assert active_hypothesis is not None  # noqa: S101  # started above
                    plan = active_hypothesis.plan
                    _persist_agent_run_state(
                        ctx,
                        state_store,
                        agent_run_state,
                        label=f"agent: start hypothesis {plan.hypothesis_id}",
                    )
                    ctx.events.emit(
                        CoreEventType.EXPERIMENTS_CHANGED,
                        data=ExperimentsChangedData(reason="active_hypothesis_changed"),
                    )
                else:
                    plan = active_hypothesis.plan
                    issue_board.append_hypothesis_continuation(
                        progress_path,
                        round_number,
                        plan=plan,
                        started_round=active_hypothesis.started_round,
                        continuation_step=active_hypothesis.next_step or plan.task,
                    )
                    ctx.lprint(
                        f"[hypothesis] continuing {plan.hypothesis_id}; designer invocation skipped"
                    )

                planned_official_reason = _official_evaluation_reason(
                    records=records,
                    round_number=round_number,
                    max_rounds=max_rounds,
                    official_eval_every=official_eval_every,
                    requested=plan.request_official_evaluation,
                    candidate_ready=True,
                )

                # No early stop: the loop always consumes the full max_rounds
                # budget. Previously OrchestratorPlan had a ``done`` field that
                # could halt the loop; it was removed because the orchestrator
                # can't reliably tell when the objective is "fully met" and
                # early-stopping masks further optimization opportunities.

                # --- Optional rollback ---
                if plan.revert_to_round is not None and not active_hypothesis.revert_applied:
                    target = next(
                        (r for r in records if r.round_number == plan.revert_to_round),
                        None,
                    )
                    if target and target.commit:
                        rollback_commit, failed_child_round = round_history.resolve_rollback_commit(
                            target, _FAILED_HYPOTHESIS_OUTCOMES
                        )
                        assert rollback_commit is not None  # noqa: S101  # tracked: #288
                        # Restore the tree without moving HEAD so subsequent
                        # commits land on the current branch as new commits
                        # after the reverted state.
                        memory_paths = tuple(
                            str(path.relative_to(ctx.workspace))
                            for path in (roadmap_path, progress_path, pareto_archive_path)
                        )
                        if ctx.git.checkout_tree(
                            rollback_commit,
                            clean=True,
                            preserve_paths=memory_paths,
                        ):
                            if failed_child_round is None:
                                ctx.lprint(
                                    "Reverted workspace to round "
                                    f"{plan.revert_to_round} ({rollback_commit[:8]})."
                                )
                            else:
                                ctx.lprint(
                                    "Reverted workspace to the pre-hypothesis parent of "
                                    f"failed round {failed_child_round} ({rollback_commit[:8]}), "
                                    f"based on parent round {plan.revert_to_round}."
                                )
                            active_hypothesis.revert_applied = True
                            active_hypothesis.revert_commit = rollback_commit
                            active_hypothesis.parent_commit = rollback_commit
                            agent_run_state = _persist_active_hypothesis(
                                ctx,
                                state_store,
                                agent_run_state,
                                active_hypothesis,
                                label=(f"agent: set hypothesis {plan.hypothesis_id} parent"),
                            )
                        else:
                            ctx.lprint(
                                "[warn] rollback was not applied; will retry round "
                                f"{plan.revert_to_round} on the next continuation"
                            )
                    else:
                        ctx.lprint(
                            f"[warn] cannot revert: no commit recorded for round {plan.revert_to_round}"
                        )

                # --- Implementer / Judge retry loop ---
                # Round-scoped accumulators: these describe the round as a
                # whole and intentionally survive every attempt (the best
                # accepted implementation, the official evaluation the round
                # completed, and the feedback carried forward). A failed review
                # can carry targeted feedback into the same persistent
                # hypothesis on the next framework round.
                feedback: str | None = active_hypothesis.feedback
                passed = False
                review_started = False
                framework_revalidation_required = active_hypothesis.gate_revalidation_pending
                implementation: ImplementerResponse | None = None
                single_agent_response: SingleAgentRoundResponse | None = None
                framework_perf_metric: float | None = None
                framework_benchmark = FrameworkBenchmarkOutcome()
                accepted_metrics: dict[str, float] = {}
                accepted_evaluation_artifact: str | None = None
                completed_official_evaluation_reason: str | None = None
                # Attempt-scoped state: one immutable value describing the
                # judge's treatment of the current attempt only. It is replaced
                # wholesale at the top of every iteration and never updated in
                # place, so an earlier attempt's verdict cannot survive into
                # the round record (issue #503).
                attempt_judge: JudgeOutcome = JudgeSkipped(JudgeSkipReason.NOT_REACHED)
                # ``max_retries_per_round >= 1`` is validated at entry, so the
                # loop always runs; the initializer keeps ``retry`` provably
                # bound for the post-loop round bookkeeping.
                retry = 0
                first_retry = issue_board.next_implementer_attempt(progress_path, round_number)
                if first_retry > max_retries_per_round:
                    raise RuntimeError(  # noqa: TRY003  # tracked: #288
                        f"Round {round_number} already persisted "
                        f"{first_retry - 1} implementer attempts, exhausting "
                        f"max_retries_per_round={max_retries_per_round}; refusing "
                        "to overwrite or replay paid work."
                    )
                if first_retry > 1:
                    ctx.lprint(
                        f"[resume] round {round_number} continues at durable "
                        f"attempt {first_retry}/{max_retries_per_round}"
                    )
                for retry in range(first_retry, max_retries_per_round + 1):
                    ctx.lprint(f"\n--- attempt {retry}/{max_retries_per_round} ---\n")
                    # The persisted record must reflect this attempt, not an
                    # earlier audit.  A cadence review of attempt N returns a
                    # verdict; if the final attempt N+1 defers re-review (the
                    # sparse-review break below), that verdict belongs to a
                    # different implementation and must not survive.
                    attempt_judge = JudgeSkipped(JudgeSkipReason.NOT_REACHED)
                    if inner_loop == "multi-agent":
                        prior_attempt_artifact_locations = tuple(
                            issue_board.display_path(path, ctx.workspace)
                            for path in issue_board.implementer_artifact_paths(
                                progress_path, round_number
                            )
                        )
                        ctx.reselect_gpu()
                        attempt = _run_implementer(
                            ctx,
                            round_number=round_number,
                            retry=retry,
                            plan=plan,
                            objective=objective,
                            modality=modality,
                            interface=interface,
                            domain_definition=domain_definition,
                            feedback=feedback,
                            continuation_step=active_hypothesis.next_step,
                            framework_revert_applied=active_hypothesis.revert_applied,
                            framework_revert_round=active_hypothesis.parent_round,
                            framework_revert_commit=active_hypothesis.revert_commit,
                            gate_revalidation_pending=(active_hypothesis.gate_revalidation_pending),
                            gate_approved_perf_metric=(active_hypothesis.gate_approved_perf_metric),
                            gate_approved_perf_unit=(active_hypothesis.gate_approved_perf_unit),
                            gate_approved_evaluation_artifact=(
                                active_hypothesis.gate_approved_evaluation_artifact
                            ),
                            progress_path=progress_path,
                            progress_location=progress_location,
                            pareto_archive_location=pareto_archive_location,
                            framework_benchmark_enabled=framework_benchmark_configured,
                            official_evaluation_due=(planned_official_reason is not None),
                            official_evaluation_reason=planned_official_reason,
                            prior_attempt_artifact_locations=prior_attempt_artifact_locations,
                        )
                        implementation = attempt.response
                        if attempt.synthesized:
                            # The framework, not the implementer, wrote this
                            # response because the turn's output did not parse.
                            # It carries no reviewable evidence, so spend a
                            # retry on a real turn instead of letting a parse
                            # failure silently consume the whole round. When
                            # retries are exhausted the round still commits the
                            # fail-closed response, unreviewed, as before.
                            attempt_judge = JudgeSkipped(JudgeSkipReason.UNPARSEABLE_IMPLEMENTATION)
                            ctx.lprint(
                                f"[implementer] attempt {retry}/{max_retries_per_round} "
                                "returned no parseable structured response; the framework "
                                "synthesized a fail-closed one. "
                                + (
                                    "Retrying within the same round."
                                    if retry < max_retries_per_round
                                    else "Retries are exhausted; completing the round with it."
                                )
                            )
                            continue
                        review_due = _review_due(
                            round_number=round_number,
                            max_rounds=max_rounds,
                            judge_every=judge_every,
                            outcome=implementation.hypothesis_outcome,
                            candidate_evidence_fresh=_candidate_evidence_is_fresh(
                                implementation, records
                            ),
                        )
                        if review_started and not _implementation_requests_continuation(
                            implementation
                        ):
                            # Re-review a terminal response to feedback from a
                            # failed judge. Sparse cadence controls the first
                            # audit of a round, not whether the resulting repair
                            # is independently verified.
                            review_due = True
                        if (
                            review_started
                            and round_number != max_rounds
                            and _implementation_requests_continuation(implementation)
                            and implementation.candidate_disposition
                            is not CandidateDisposition.PARETO_FRONTIER
                            and not framework_revalidation_required
                        ):
                            # A cadence-triggered review has already supplied
                            # feedback for this round.  Let a provisional retry
                            # that still owns the hypothesis continue in the
                            # next framework round instead of paying for the
                            # same independent audit twice.  A terminal retry
                            # must be re-reviewed: otherwise a judge-requested
                            # repair could be accepted without independent
                            # verification merely because sparse cadence is not
                            # due again.
                            review_due = False
                        if not review_due:
                            attempt_judge = JudgeSkipped(JudgeSkipReason.SPARSE_REVIEW_POLICY)
                            issue_board.append_judge_skipped(
                                progress_path,
                                round_number,
                                outcome=implementation.hypothesis_outcome.value,
                                judge_every=judge_every,
                            )
                            ctx.lprint(
                                "[judge] deferred by sparse-review policy; "
                                "official gates were not run"
                            )
                            break
                        review_started = True
                        framework_revalidation_required = False
                        candidate_archive_conflict = _pareto_archive_conflict(
                            candidate_disposition=implementation.candidate_disposition,
                            candidate_metrics=dict(implementation.candidate_metrics),
                            records=records,
                            space=agent_run_state.metrics,
                        )
                        ctx.reselect_gpu()
                        verdict = _run_judge(
                            ctx,
                            round_number=round_number,
                            retry=retry,
                            plan=plan,
                            implementation=implementation,
                            modality=modality,
                            interface=interface,
                            domain_definition=domain_definition,
                            progress_path=progress_path,
                            progress_location=progress_location,
                            pareto_archive_location=pareto_archive_location,
                            objective=objective,
                            framework_revert_applied=active_hypothesis.revert_applied,
                            framework_revert_round=active_hypothesis.parent_round,
                            framework_revert_commit=active_hypothesis.revert_commit,
                            gate_revalidation_pending=(active_hypothesis.gate_revalidation_pending),
                            gate_approved_perf_metric=(active_hypothesis.gate_approved_perf_metric),
                            gate_approved_perf_unit=(active_hypothesis.gate_approved_perf_unit),
                            gate_approved_metrics=active_hypothesis.gate_approved_metrics,
                            gate_approved_evaluation_artifact=(
                                active_hypothesis.gate_approved_evaluation_artifact
                            ),
                            framework_benchmark_enabled=framework_benchmark_configured,
                            official_evaluation_due=(planned_official_reason is not None),
                            official_evaluation_reason=planned_official_reason,
                            pareto_archive_conflict=candidate_archive_conflict,
                        )
                        attempt_judge = JudgeReviewed(verdict.verdict)
                        if verdict.verdict == Verdict.PASS:
                            validation_feedback = _run_framework_validation_gate(
                                ctx,
                                recipe_artifact=implementation.validation_recipe_artifact,
                                round_number=round_number,
                                retry=retry,
                                progress_path=progress_path,
                            )
                            if validation_feedback is not None:
                                feedback = validation_feedback
                                active_hypothesis.feedback = feedback
                                agent_run_state = _persist_active_hypothesis(
                                    ctx,
                                    state_store,
                                    agent_run_state,
                                    active_hypothesis,
                                    label=f"agent: checkpoint hypothesis {plan.hypothesis_id}",
                                )
                                continue
                            if (
                                implementation.candidate_disposition
                                is CandidateDisposition.PARETO_FRONTIER
                            ):
                                active_hypothesis.gate_approved_candidate_disposition = (
                                    implementation.candidate_disposition.value
                                )
                                active_hypothesis.gate_approved_candidate_metrics = dict(
                                    implementation.candidate_metrics
                                )
                                active_hypothesis.gate_approved_candidate_evaluation_artifact = (
                                    implementation.candidate_evaluation_artifact
                                )
                                active_hypothesis.gate_approved_candidate_operating_point = (
                                    implementation.candidate_operating_point
                                )
                                active_hypothesis.gate_approved_candidate_retention_reason = (
                                    implementation.candidate_retention_reason
                                )
                                agent_run_state = _persist_active_hypothesis(
                                    ctx,
                                    state_store,
                                    agent_run_state,
                                    active_hypothesis,
                                    label=f"agent: checkpoint hypothesis {plan.hypothesis_id}",
                                )
                            candidate_ready = implementation.hypothesis_outcome in {
                                HypothesisOutcome.SUPPORTED,
                                HypothesisOutcome.NOMINATED,
                            } or (
                                implementation.candidate_disposition
                                is CandidateDisposition.PARETO_FRONTIER
                            )
                            official_reason = _official_evaluation_reason(
                                records=records,
                                round_number=round_number,
                                max_rounds=max_rounds,
                                official_eval_every=official_eval_every,
                                requested=plan.request_official_evaluation,
                                candidate_ready=candidate_ready,
                            )
                            if official_reason is None:
                                if candidate_ready:
                                    issue_board.append_official_evaluation_decision(
                                        progress_path,
                                        round_number,
                                        retry,
                                        run=False,
                                        reason="cadence_not_due",
                                        official_eval_every=official_eval_every,
                                        provisional_candidates=(
                                            _provisional_candidates_since_official(records)
                                        ),
                                    )
                                    ctx.lprint(
                                        "[official-evaluation] deferred; candidate "
                                        "retained as a provisional working checkpoint"
                                    )
                                passed = True
                                break
                            if implementation.perf_metric is not None:
                                active_hypothesis.gate_approved_perf_metric = (
                                    implementation.perf_metric
                                )
                                active_hypothesis.gate_approved_perf_unit = implementation.perf_unit
                                active_hypothesis.gate_approved_metrics = dict(
                                    implementation.metrics
                                )
                                active_hypothesis.gate_approved_evaluation_artifact = (
                                    implementation.evaluation_artifact
                                )
                                agent_run_state = _persist_active_hypothesis(
                                    ctx,
                                    state_store,
                                    agent_run_state,
                                    active_hypothesis,
                                    label=f"agent: checkpoint hypothesis {plan.hypothesis_id}",
                                )
                            issue_board.append_official_evaluation_decision(
                                progress_path,
                                round_number,
                                retry,
                                run=True,
                                reason=official_reason,
                                official_eval_every=official_eval_every,
                                provisional_candidates=(
                                    _provisional_candidates_since_official(records)
                                ),
                            )
                            candidate_commit = ctx.git.current_sha()
                            reuse_accuracy_pass = bool(
                                active_hypothesis.gate_revalidation_pending
                                and candidate_commit is not None
                                and active_hypothesis.gate_candidate_commit == candidate_commit
                                and active_hypothesis.gate_accuracy_passed
                            )
                            (
                                gate_feedback,
                                framework_benchmark,
                                accuracy_passed,
                            ) = _run_framework_gates(
                                ctx,
                                benchmark_result=benchmark_result,
                                benchmark_result_protocol=benchmark_result_protocol,
                                objectives=objectives,
                                round_number=round_number,
                                retry=retry,
                                progress_path=progress_path,
                                accuracy_timeout_seconds=accuracy_timeout_seconds,
                                benchmark_timeout_seconds=benchmark_timeout_seconds,
                                reuse_accuracy_pass=reuse_accuracy_pass,
                                candidate_revision=candidate_commit,
                            )
                            framework_perf_metric = framework_benchmark.metric_value
                            if gate_feedback is None:
                                passed = True
                                completed_official_evaluation_reason = official_reason
                                break
                            feedback = gate_feedback
                            framework_revalidation_required = True
                            active_hypothesis.gate_revalidation_pending = True
                            active_hypothesis.gate_candidate_commit = candidate_commit
                            active_hypothesis.gate_accuracy_passed = accuracy_passed
                            active_hypothesis.feedback = feedback
                            agent_run_state = _persist_active_hypothesis(
                                ctx,
                                state_store,
                                agent_run_state,
                                active_hypothesis,
                                label=f"agent: checkpoint hypothesis {plan.hypothesis_id}",
                            )
                            continue
                        feedback = verdict.feedback
                        active_hypothesis.feedback = feedback
                        agent_run_state = _persist_active_hypothesis(
                            ctx,
                            state_store,
                            agent_run_state,
                            active_hypothesis,
                            label=f"agent: checkpoint hypothesis {plan.hypothesis_id}",
                        )
                    else:
                        ctx.reselect_gpu()
                        single_agent_response = _run_single_agent_round(
                            ctx,
                            round_number=round_number,
                            retry=retry,
                            plan=plan,
                            modality=modality,
                            interface=interface,
                            domain_definition=domain_definition,
                            feedback=feedback,
                            progress_path=progress_path,
                            progress_location=progress_location,
                            pareto_archive_location=pareto_archive_location,
                            objective=objective,
                            profile_focus=last_profile_focus,
                            official_evaluation_due=(planned_official_reason is not None),
                            official_evaluation_reason=planned_official_reason,
                            framework_benchmark_enabled=framework_benchmark_configured,
                            pareto_records=records,
                            space=agent_run_state.metrics,
                        )
                        attempt_judge = JudgeReviewed(single_agent_response.verdict)
                        if single_agent_response.verdict == Verdict.PASS:
                            official_reason = _official_evaluation_reason(
                                records=records,
                                round_number=round_number,
                                max_rounds=max_rounds,
                                official_eval_every=official_eval_every,
                                requested=plan.request_official_evaluation,
                                candidate_ready=True,
                            )
                            if official_reason is None:
                                issue_board.append_official_evaluation_decision(
                                    progress_path,
                                    round_number,
                                    retry,
                                    run=False,
                                    reason="cadence_not_due",
                                    official_eval_every=official_eval_every,
                                    provisional_candidates=(
                                        _provisional_candidates_since_official(records)
                                    ),
                                )
                                ctx.lprint(
                                    "[official-evaluation] deferred; candidate "
                                    "retained as a provisional working checkpoint"
                                )
                                passed = True
                                break
                            issue_board.append_official_evaluation_decision(
                                progress_path,
                                round_number,
                                retry,
                                run=True,
                                reason=official_reason,
                                official_eval_every=official_eval_every,
                                provisional_candidates=(
                                    _provisional_candidates_since_official(records)
                                ),
                            )
                            candidate_commit = ctx.git.current_sha()
                            reuse_accuracy_pass = bool(
                                active_hypothesis.gate_revalidation_pending
                                and candidate_commit is not None
                                and active_hypothesis.gate_candidate_commit == candidate_commit
                                and active_hypothesis.gate_accuracy_passed
                            )
                            (
                                gate_feedback,
                                framework_benchmark,
                                accuracy_passed,
                            ) = _run_framework_gates(
                                ctx,
                                benchmark_result=benchmark_result,
                                benchmark_result_protocol=benchmark_result_protocol,
                                objectives=objectives,
                                round_number=round_number,
                                retry=retry,
                                progress_path=progress_path,
                                accuracy_timeout_seconds=accuracy_timeout_seconds,
                                benchmark_timeout_seconds=benchmark_timeout_seconds,
                                reuse_accuracy_pass=reuse_accuracy_pass,
                                candidate_revision=candidate_commit,
                            )
                            framework_perf_metric = framework_benchmark.metric_value
                            if gate_feedback is None:
                                passed = True
                                completed_official_evaluation_reason = official_reason
                                break
                            feedback = gate_feedback
                            active_hypothesis.gate_revalidation_pending = True
                            active_hypothesis.gate_candidate_commit = candidate_commit
                            active_hypothesis.gate_accuracy_passed = accuracy_passed
                            active_hypothesis.feedback = feedback
                            agent_run_state = _persist_active_hypothesis(
                                ctx,
                                state_store,
                                agent_run_state,
                                active_hypothesis,
                                label=f"agent: checkpoint hypothesis {plan.hypothesis_id}",
                            )
                            continue
                        feedback = single_agent_response.feedback
                        active_hypothesis.feedback = feedback
                        agent_run_state = _persist_active_hypothesis(
                            ctx,
                            state_store,
                            agent_run_state,
                            active_hypothesis,
                            label=f"agent: checkpoint hypothesis {plan.hypothesis_id}",
                        )

                # --- Record round result & update carry-over ---
                # Only the final attempt describes the round, so its outcome is
                # the one the record and the lifecycle transition read.
                final_attempt_reviewed = attempt_was_reviewed(attempt_judge)
                commit = ctx.git.current_sha()
                # `profile_skipped` is True when no fresh profile ran this round
                # (cold-start or the orchestrator/framework decided to skip).
                # The plateau detector ignores skipped-profile rounds so cached
                # / inherited perf numbers don't masquerade as fresh measurements.
                #
                # For single-agent inner loop, `profiler_summary` carries the
                # PREVIOUS round's profile (fed forward to the orchestrator),
                # so this round's perf comes from `single_agent_response` instead.
                perf_provenance: PerfProvenance | None = None
                if inner_loop == "single-agent":
                    if (
                        single_agent_response is not None
                        and framework_perf_metric is not None
                        and completed_official_evaluation_reason is not None
                    ):
                        single_agent_response.perf_metric = framework_perf_metric
                        single_agent_response.perf_unit = framework_benchmark.metric_name
                        perf_provenance = "framework"
                    profile_skipped = single_agent_response is None or (
                        single_agent_response.perf_metric is None
                    )
                    perf_metric = (
                        single_agent_response.perf_metric
                        if (
                            single_agent_response
                            and passed
                            and completed_official_evaluation_reason is not None
                        )
                        else None
                    )
                    perf_unit = (
                        single_agent_response.perf_unit
                        if (
                            single_agent_response
                            and passed
                            and completed_official_evaluation_reason is not None
                        )
                        else None
                    )
                    if perf_metric is not None and perf_provenance is None:
                        # Not overridden by the framework benchmark above, so
                        # this headline number is the agent's own report.
                        perf_provenance = "implementer"
                    # Remember the latest profile for the orchestrator's next plan
                    # and carry forward the implicit profile focus.
                    if single_agent_response is not None:
                        last_single_agent_response = single_agent_response
                else:
                    implementation_metric = (
                        implementation.perf_metric
                        if (
                            implementation is not None
                            and passed
                            and completed_official_evaluation_reason is not None
                        )
                        else None
                    )
                    if (
                        implementation_metric is None
                        and passed
                        and implementation is not None
                        and implementation.hypothesis_outcome
                        in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
                        and active_hypothesis.gate_revalidation_pending
                        and completed_official_evaluation_reason is not None
                    ):
                        implementation_metric = active_hypothesis.gate_approved_perf_metric
                    profile_skipped = (
                        framework_perf_metric is None and implementation_metric is None
                    )
                    if (
                        framework_perf_metric is not None
                        and passed
                        and completed_official_evaluation_reason is not None
                    ):
                        perf_metric = framework_perf_metric
                        perf_unit = framework_benchmark.metric_name
                        perf_provenance = "framework"
                    elif implementation_metric is not None:
                        perf_metric = implementation_metric
                        perf_provenance = "implementer"
                        if implementation is not None and implementation.perf_metric is not None:
                            perf_unit = implementation.perf_unit
                            accepted_metrics = dict(implementation.metrics)
                            accepted_evaluation_artifact = implementation.evaluation_artifact
                        else:
                            perf_unit = active_hypothesis.gate_approved_perf_unit
                            accepted_metrics = dict(active_hypothesis.gate_approved_metrics)
                            accepted_evaluation_artifact = (
                                active_hypothesis.gate_approved_evaluation_artifact
                            )
                    else:
                        # Profiles and directional probes inform the designer,
                        # but only an official checkpoint may update the
                        # verified headline trajectory in portable round records.
                        perf_metric = None
                        perf_unit = None
                # A prior retry may have been reviewed before the implementer
                # returned a different terminal result.  Record and transition
                # from the final attempt, not from any earlier audit in the
                # round; otherwise an unreviewed ``disproven`` retry is
                # mislabeled rejected and the dead hypothesis stays active.
                reviewed = final_attempt_reviewed if inner_loop == "multi-agent" else True
                if implementation is not None:
                    candidate_disposition = implementation.candidate_disposition.value
                    candidate_metrics = dict(implementation.candidate_metrics)
                    candidate_evaluation_artifact = implementation.candidate_evaluation_artifact
                    candidate_operating_point = implementation.candidate_operating_point
                    candidate_retention_reason = implementation.candidate_retention_reason
                elif single_agent_response is not None:
                    candidate_disposition = single_agent_response.candidate_disposition.value
                    candidate_metrics = dict(single_agent_response.candidate_metrics)
                    candidate_evaluation_artifact = (
                        single_agent_response.candidate_evaluation_artifact
                    )
                    candidate_operating_point = single_agent_response.candidate_operating_point
                    candidate_retention_reason = single_agent_response.candidate_retention_reason
                else:
                    candidate_disposition = CandidateDisposition.UNASSESSED.value
                    candidate_metrics = {}
                    candidate_evaluation_artifact = None
                    candidate_operating_point = ""
                    candidate_retention_reason = ""

                # A framework-gate retry may correctly return no fresh candidate
                # row. Preserve the judge-approved provisional evidence for the
                # unchanged checkpoint just as canonical evidence is preserved.
                if (
                    candidate_disposition == CandidateDisposition.UNASSESSED.value
                    and active_hypothesis.gate_revalidation_pending
                    and active_hypothesis.gate_approved_candidate_disposition
                    == CandidateDisposition.PARETO_FRONTIER.value
                ):
                    candidate_disposition = active_hypothesis.gate_approved_candidate_disposition
                    candidate_metrics = dict(active_hypothesis.gate_approved_candidate_metrics)
                    candidate_evaluation_artifact = (
                        active_hypothesis.gate_approved_candidate_evaluation_artifact
                    )
                    candidate_operating_point = (
                        active_hypothesis.gate_approved_candidate_operating_point
                    )
                    candidate_retention_reason = (
                        active_hypothesis.gate_approved_candidate_retention_reason
                    )
                # A result-protocol benchmark measures the complete objective
                # row, so it replaces the provisional metrics outright.
                if perf_metric is not None and framework_benchmark.row is not None:
                    accepted_metrics = dict(framework_benchmark.row)
                # A legacy [benchmark.result] benchmark reports a single trusted
                # scalar via perf_metric/perf_unit but leaves accepted_metrics
                # empty (only implementer-reported evals populate it). Without a
                # comparable objective row, _record_candidate_metrics falls back
                # to the provisional candidate_metrics for frontier dominance.
                # Promote the trusted scalar into the objective row so official
                # framework measurements drive the frontier. Does not apply when
                # a protocol row is present: the row above is already complete.
                if not accepted_metrics and perf_metric is not None and perf_unit is not None:
                    accepted_metrics = {perf_unit: perf_metric}
                official_evaluation = (
                    passed
                    and completed_official_evaluation_reason is not None
                    and ctx.agent_client.backend_name != "stub"
                    and (bool(ctx.judge_accuracy_command) or framework_benchmark_configured)
                )
                declared_outcome = (
                    implementation.hypothesis_outcome
                    if implementation is not None
                    else HypothesisOutcome.NOMINATED
                    if single_agent_response is not None
                    else None
                )
                primary_objective = objectives[0] if objectives else None
                metric_name = (
                    framework_benchmark.metric_name
                    or (primary_objective.name if primary_objective is not None else None)
                    or perf_unit
                )
                metric_direction = framework_benchmark.metric_direction or (
                    primary_objective.direction if primary_objective is not None else None
                )
                official_metric = (
                    accepted_metrics.get(metric_name) if metric_name is not None else perf_metric
                )
                if official_metric is None and not accepted_metrics:
                    official_metric = perf_metric
                parent_record = metric_baseline(
                    parent_round=active_hypothesis.parent_round,
                    parent_commit=active_hypothesis.parent_commit,
                    metric=metric_name,
                    rounds=records,
                )
                baseline_metric = (
                    record_metric_value(parent_record, metric_name)
                    if parent_record is not None
                    else None
                )
                # A headline metric is framework-owned unless the implementer
                # self-reported it. This is the trust boundary the rest of the
                # round applies: resolution, scalar and Pareto retention, the
                # recorded delta, and trusted Pareto-parent selection all read
                # it, so an untrusted number never drives a dominance decision.
                framework_provenance = trusted_perf_provenance(perf_provenance)
                # The round's headline reading is ordered against its causal
                # baseline exactly once, here, and stored on the record. Every
                # later reader -- resume reprojection and the server -- consumes
                # the stored answer instead of re-deriving it.
                #
                # An implementer-reported number is never ordered at all: the
                # comparison stays None, which is what makes the hypothesis
                # resolve UNMEASURED rather than borrowing a verdict from a
                # number the framework did not measure.
                space = agent_run_state.metrics
                official_reading = (
                    Measurement(
                        metric=metric_name,
                        value=official_metric,
                        direction=metric_direction,
                    )
                    if metric_name is not None and official_metric is not None
                    else None
                )
                perf_comparison = (
                    space.compare(
                        official_reading,
                        Measurement(
                            metric=metric_name,
                            value=baseline_metric,
                            direction=metric_direction,
                        )
                        if metric_name is not None and baseline_metric is not None
                        else None,
                    )
                    if official_evaluation and official_metric is not None and framework_provenance
                    else None
                )
                hypothesis_resolution = resolve_hypothesis_outcome(
                    ResolutionEvidence(
                        declared=declared_outcome,
                        passed=passed,
                        reviewed=reviewed,
                        comparison=perf_comparison,
                    )
                )
                disposition = CandidateDisposition(candidate_disposition)
                if not reviewed:
                    candidate_retained = _provisional_candidate_retained(disposition)
                elif not passed:
                    candidate_retained = False
                elif (
                    official_evaluation and framework_provenance and objectives and accepted_metrics
                ):
                    candidate_retained = not _pareto_archive_dominators(
                        accepted_metrics,
                        records,
                        space,
                    )
                elif official_evaluation and framework_provenance:
                    prior_readings = [
                        Measurement(
                            metric=metric_name,
                            value=value,
                            direction=metric_direction,
                        )
                        for record in records
                        if metric_name is not None
                        and record.official_evaluation
                        and trusted_perf_provenance(record.perf_provenance)
                        and (value := record_metric_value(record, metric_name)) is not None
                    ]
                    candidate_retained = scalar_candidate_retained(
                        space.compare_to_best(official_reading, prior_readings)
                    )
                else:
                    # No trusted framework measurement (or an implementer
                    # self-report): retain provisionally on the implementer's
                    # disposition, never on the untrusted metric.
                    candidate_retained = _provisional_candidate_retained(disposition)
                perf_delta_pct = None
                if (
                    framework_provenance
                    and official_metric is not None
                    and baseline_metric is not None
                    and baseline_metric != 0
                ):
                    perf_delta_pct = (
                        (official_metric - baseline_metric) / abs(baseline_metric) * 100
                    )
                completed_record = RoundRecord(
                    round_number=round_number,
                    commit=commit,
                    perf_metric=perf_metric,
                    perf_unit=perf_unit,
                    passed=passed,
                    profile_skipped=profile_skipped,
                    hypothesis_id=plan.hypothesis_id,
                    hypothesis_declared_outcome=(
                        declared_outcome.value if declared_outcome is not None else None
                    ),
                    # ``RoundRecord.reviewed`` derives from this verdict, so
                    # the record cannot claim a verdict it never received.
                    judge_verdict=recorded_judge_verdict(attempt_judge),
                    hypothesis_outcome=(
                        hypothesis_resolution.value
                        if hypothesis_resolution is not None
                        else declared_outcome.value
                        if declared_outcome is not None
                        else None
                    ),
                    hypothesis_claim=plan.hypothesis or None,
                    hypothesis_task=plan.task or None,
                    hypothesis_parent_round=active_hypothesis.parent_round,
                    hypothesis_parent_commit=active_hypothesis.parent_commit,
                    metrics=accepted_metrics,
                    evaluation_artifact=accepted_evaluation_artifact,
                    official_evaluation=official_evaluation,
                    official_evaluation_reason=(
                        completed_official_evaluation_reason
                        if (
                            ctx.agent_client.backend_name != "stub"
                            and (bool(ctx.judge_accuracy_command) or framework_benchmark_configured)
                        )
                        else None
                    ),
                    candidate_disposition=candidate_disposition,
                    candidate_metrics=candidate_metrics,
                    candidate_evaluation_artifact=candidate_evaluation_artifact,
                    candidate_operating_point=candidate_operating_point,
                    candidate_retention_reason=candidate_retention_reason,
                    candidate_retained=candidate_retained,
                    perf_direction=metric_direction,
                    perf_baseline_round=(
                        parent_record.round_number if parent_record is not None else None
                    ),
                    perf_baseline_commit=(
                        parent_record.commit if parent_record is not None else None
                    ),
                    perf_baseline_metric=baseline_metric,
                    perf_delta_pct=perf_delta_pct,
                    perf_comparison=perf_comparison,
                    perf_provenance=perf_provenance,
                    implementer_driver=ctx.agent_client.driver_name,
                    implementer_provider=ctx.agent_client.provider,
                    implementer_model=ctx.agent_client.model_for_kind("implementer"),
                )
                # Compute the completed lifecycle transition in memory so its
                # exact representation can enter the write-ahead journal before
                # progress notes or durable state are mutated.
                next_active_hypothesis = active_hypothesis.clone()
                if inner_loop == "multi-agent" and _implementation_keeps_hypothesis_active(
                    implementation,
                    continuation_rounds=next_active_hypothesis.continuation_rounds,
                ):
                    next_active_hypothesis.feedback = feedback if reviewed and not passed else None
                    assert implementation is not None  # noqa: S101  # tracked: #288
                    next_active_hypothesis.next_step = implementation.next_step
                    next_active_hypothesis.continuation_rounds += 1
                elif passed:
                    next_active_hypothesis = None
                elif (
                    reviewed
                    and next_active_hypothesis.continuation_rounds
                    < _MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW
                ):
                    # A rejected review may justify another scoped repair, but
                    # it consumes the same bounded ownership lease as an
                    # implementer-declared continuation. Otherwise repeated
                    # judge failures can bypass the designer indefinitely.
                    next_active_hypothesis.feedback = feedback
                    next_active_hypothesis.next_step = (
                        implementation.next_step
                        if implementation is not None
                        and _implementation_requests_continuation(implementation)
                        else None
                    )
                    next_active_hypothesis.continuation_rounds += 1
                elif reviewed or (
                    implementation is not None
                    and not _implementation_keeps_hypothesis_active(
                        implementation,
                        continuation_rounds=next_active_hypothesis.continuation_rounds,
                    )
                ):
                    next_active_hypothesis = None
                else:
                    next_active_hypothesis.feedback = None
                    next_active_hypothesis.next_step = (
                        implementation.next_step if implementation is not None else None
                    )
                state_before_round = (
                    update_active_hypothesis(agent_run_state, next_active_hypothesis)
                    if next_active_hypothesis is not None
                    else agent_run_state
                )
                next_agent_run_state = append_round(
                    state_before_round,
                    completed_record,
                    keep_active=next_active_hypothesis is not None,
                )
                state_transition = state_store.transition(next_agent_run_state)
                ctx.begin_completed_round(
                    round_number,
                    state_transition=state_transition,
                )
                records.append(completed_record)

                if not passed and records[-1].reviewed:
                    issue_board.append_exhaustion_note(
                        progress_path,
                        round_number,
                        max_retries_per_round,
                        feedback or "",
                    )
                    carry.exhaustion_info = (
                        f"Round {round_number} did not pass after "
                        f"{max_retries_per_round} attempts. Last judge feedback: "
                        f"{feedback or '(empty)'}"
                    )
                    carry.regression_info = None
                elif passed:
                    carry.exhaustion_info = None
                    if (
                        inner_loop == "multi-agent"
                        and implementation is not None
                        and not _implementation_keeps_hypothesis_active(
                            implementation,
                            continuation_rounds=active_hypothesis.continuation_rounds,
                        )
                        and implementation.hypothesis_outcome is not HypothesisOutcome.NOMINATED
                    ):
                        # A reviewed terminal classification is accepted, but
                        # its implementation edits are still in the workspace.
                        # Give the next designer the same explicit parent-state
                        # decision as an unreviewed terminal result.
                        carry.regression_info = _terminal_workspace_notice(records)
                    elif official_evaluation and candidate_retained is False:
                        carry.regression_info = (
                            f"Round {round_number}'s official candidate was not retained: "
                            f"{perf_metric}{(' ' + perf_unit) if perf_unit else ''}. "
                            "Use its recorded parent and objective directions when choosing "
                            "the next checkpoint."
                        )
                    else:
                        carry.regression_info = None
                else:
                    # A provisional round is normal hypothesis work, not a
                    # judge-loop exhaustion or a performance regression.
                    carry.exhaustion_info = None
                    carry.regression_info = (
                        None
                        if _implementation_keeps_hypothesis_active(
                            implementation,
                            continuation_rounds=active_hypothesis.continuation_rounds,
                        )
                        else _terminal_workspace_notice(records)
                    )

                # The framework, rather than the designer, owns this lifecycle.
                # A continuing implementation keeps its plan and session. An
                # unreviewed terminal result hands control back to the designer;
                # a rejected review keeps the same claim plus reviewer feedback
                # so the implementer can address it on the next round.
                ctx.persist_completed_round()
                agent_run_state = next_agent_run_state
                active_hypothesis = agent_run_state.active_hypothesis
                ctx.events.emit(
                    CoreEventType.EXPERIMENTS_CHANGED,
                    data=ExperimentsChangedData(reason="round_persisted"),
                )
                ctx.events.emit(
                    CoreEventType.ROUND_FINISHED,
                    status=(
                        EventStatus.COMPLETED
                        if passed or not records[-1].reviewed
                        else EventStatus.FAILED
                    ),
                    round_label=f"round-{round_number}",
                    data=RoundFinishedData(
                        attempts=retry,
                        judge_verdict=(
                            "pass" if passed else "fail" if records[-1].reviewed else "skipped"
                        ),
                        perf_metric=perf_metric,
                        perf_unit=perf_unit,
                        profile_skipped=profile_skipped,
                    ),
                )

                round_number += 1

        ctx.lprint(f"Reached max_rounds={max_rounds}. Stopping.")
        return True
    finally:
        ctx.close()
