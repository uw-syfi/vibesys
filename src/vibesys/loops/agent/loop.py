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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Generic, Literal, NotRequired, TypedDict, TypeVar, Unpack

from pydantic import BaseModel

from vibesys import constants
from vibesys.agent_spec_config import resolve_agent_driver
from vibesys.config import as_config
from vibesys.context import create_run_context
from vibesys.domains.base import DomainDefinition, DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.events import (
    CoreEventType,
    EventStatus,
    FrameworkSource,
    GateFinishedData,
    GateKind,
    JudgeResultData,
    RoundFinishedData,
    RunConfiguredData,
)
from vibesys.loops.agent import issue_board
from vibesys.loops.agent._archive import (
    _format_metric_row,
    _pareto_archive_conflict,
    _pareto_archive_dominators,
    _pareto_archive_summary,
    _pareto_frontier_records,
    _provisional_candidate_retained,
    _record_candidate_metrics,
    _record_candidate_retained,
    _select_final_candidate,
)
from vibesys.loops.agent.attempt import (
    JudgeOutcome,
    JudgeReviewed,
    JudgeSkipped,
    JudgeSkipReason,
    recorded_judge_verdict,
)
from vibesys.loops.agent.hypotheses import (
    ResolutionEvidence,
    adopt_metric_space,
    apply_strategy_updates,
    metric_baseline,
    record_metric_value,
    resolve_hypothesis_outcome,
    scalar_candidate_retained,
    trusted_perf_provenance,
    update_active_hypothesis,
)
from vibesys.loops.agent.hypothesis_controller import (
    HypothesisEngine,
    ProfileGuidanceOutcome,
    persist_active_hypothesis,
    persist_agent_run_state,
    plan_changed_keys,
    publish_experiments_changed,
)
from vibesys.loops.agent.model import (
    AgentRunState,
    Hypothesis,
    HypothesisResolution,
)
from vibesys.loops.agent.state import AgentRunStateStore
from vibesys.loops.gates import (
    GATE_LOG_TAIL_CHARS,
    GATE_RECORD_TAIL_CHARS,
    AccuracyGateResult,
    BenchmarkContract,
    FrameworkBenchmarkOutcome,
    emit_gate_finished,
    emit_gate_started,
    framework_command_timeout,
    run_accuracy_gate,
    run_benchmark_gate,
)
from vibesys.loops.metrics import (
    Measurement,
    MetricSpace,
)
from vibesys.loops.profiler import mcp_spec as profiler_mcp_spec
from vibesys.profilers import (
    ProfilerDefinition,
    ProfilerKind,
    profiler_definition,
    require_profiler_kind,
)
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.render.sink import output_sink
from vibesys.run import LocalRunIntegration, LoopContext, RunStateNamespace
from vibesys.sandbox.model_requests import ModelRequestError, reconcile_model_requests
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
from vs_agent.api import (
    AgentBackend,
    AgentSessionKey,
    MCPServerSpec,
    ResponseFallback,
    RoundProgress,
    SessionScope,
)
from vs_loop_state.api import PerfProvenance, RoundHistory, RoundRecord
from vs_project.api import AgentRunConfiguration

_ReadOnlyResponseT = TypeVar("_ReadOnlyResponseT", bound=BaseModel)


class _ReadOnlyRoleInvokeOptions(TypedDict, Generic[_ReadOnlyResponseT]):
    """Typed options forwarded to the run context's structured invoke API."""

    kind: str
    system_prompt: str
    user_prompt: str
    response_cls: type[_ReadOnlyResponseT]
    fallback_factory: Callable[[], _ReadOnlyResponseT]
    round_label: NotRequired[str]
    reuse_session: NotRequired[bool]
    mcp_servers: NotRequired[list[MCPServerSpec] | None]


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys.loops.request import LoopRunRequest
# Candidate process boundaries selected by ``--interface``. Language, tooling,
# and artifact requirements belong to the selected domain and input bundle.
_INTERFACES = ("inprocess", "service")
_HEALTH_CHECK_CRITERIA = "/health returns 200."
DEFAULT_INTERFACE = "inprocess"
_EN_DASH = "\N{EN DASH}"

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
_ROLE_CHANGE_DISPLAY_LIMIT = 8
_MAX_VALIDATION_INPUT_FILES = 4096
_MAX_VALIDATION_INPUT_BYTES = 256 * 1024 * 1024


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
            message = "no trusted retained candidate or trusted input baseline is available"
            raise RuntimeError(message)
        if not ctx.git.checkout_tree(baseline, clean=True, preserve_paths=relative_memory):
            message = f"could not restore trusted input baseline at {baseline}"
            raise RuntimeError(message)
        ctx.snapshot_workspace("agent: restore trusted input baseline")
        ctx.lprint(
            f"\nNo evaluated winner was retained. Restored trusted input baseline {baseline[:12]}."
        )
        return
    winner_commit = winner.commit
    if winner_commit is None:
        message = "selected final candidate is missing its commit"
        raise RuntimeError(message)
    ctx.git.retain_candidate(f"selected-round-{winner.round_number:04d}", winner_commit)
    if not ctx.git.checkout_tree(winner_commit, clean=True, preserve_paths=relative_memory):
        message = f"could not materialize selected round {winner.round_number} at {winner_commit}"
        raise RuntimeError(message)
    ctx.snapshot_workspace(f"agent: select round {winner.round_number}")
    metrics = (
        _format_metric_row(_record_candidate_metrics(winner), space.objectives)
        if space.objectives
        else f"{winner.perf_metric:.6g} {winner.perf_unit or ''}"
    )
    ctx.lprint(
        f"\nFinal selected candidate: round {winner.round_number}, "
        f"commit {winner_commit[:12]}, official metrics: {metrics.strip()}"
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
    """Return a warning string if recent same-unit rounds stayed steady.

    Check whether the most recent ``min_streak`` rounds
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
    """
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
        f"{rounds[0]}{_EN_DASH}{rounds[-1]}) all landed in {lo:.2f}{_EN_DASH}{hi:.2f}{unit_suffix} "
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
    profiler_summary: ProfilerSummary | None = None
    single_agent_response: SingleAgentRoundResponse | None = None


@dataclass(frozen=True)
class _AgentRoundSession:
    """Restored durable state and workspace location for a loop run."""

    run_state: AgentRunState
    history: RoundHistory
    carry: _CarryOver
    progress_path: Path
    round_number: int


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


def _official_evaluation_reason(
    *,
    records: list[RoundRecord],
    progress: RoundProgress,
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
    if progress.round_number == progress.total_rounds:
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
    **invoke_kwargs: Unpack[_ReadOnlyRoleInvokeOptions[_ReadOnlyResponseT]],
) -> _ReadOnlyResponseT:
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
        message = f"Cannot isolate {role}: workspace checkpoint is unavailable"
        raise RuntimeError(message)

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
                message = (
                    f"Cannot isolate {role}: failed to restore workspace checkpoint "
                    f"{checkpoint[:12]}"
                )
                raise RuntimeError(message)
            remaining = [path for path in ctx.git.pending_changes() if not is_allowed(path)]
            if remaining:
                message = (
                    f"Cannot isolate {role}: workspace is still modified after restore: "
                    f"{', '.join(remaining[:_ROLE_CHANGE_DISPLAY_LIMIT])}"
                )
                raise RuntimeError(message)
            shown = ", ".join(unauthorized[:_ROLE_CHANGE_DISPLAY_LIMIT])
            suffix = (
                ""
                if len(unauthorized) <= _ROLE_CHANGE_DISPLAY_LIMIT
                else f", ... (+{len(unauthorized) - _ROLE_CHANGE_DISPLAY_LIMIT} more)"
            )
            ctx.lprint(
                f"[role-isolation] reverted {len(unauthorized)} workspace change(s) "
                f"attempted by {role}: {shown}{suffix}"
            )


def _is_fresh_cold_start(round_number: int, records: list[RoundRecord]) -> bool:
    """True for round 1 of a fresh run (no prior rounds recorded)."""
    return round_number == 1 and not records


def _run_pre_round_decision(
    ctx: LoopContext,
    *,
    request: LoopRunRequest,
    progress: RoundProgress,
    carry: _CarryOver,
    has_history: bool,
) -> PreRoundDecision:
    round_number = progress.round_number
    objective = request.objective or request.input_bundle.objective
    progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1]
    progress_location = issue_board.display_path(progress_path, ctx.workspace)
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


def _effective_profiler_definition(
    profiler_kind: ProfilerKind,
    *,
    supports_torch_profiler: bool = False,
) -> ProfilerDefinition:
    """Return the already-resolved profiler declaration.

    Context creation resolves the requested profiler against both the domain
    and the run environment's declared capabilities.  Do not perform a second
    interface-based substitution here: it can replace a supported remote
    capture path with a profiler that the environment cannot execute.
    """
    kind = require_profiler_kind(profiler_kind)
    if kind is ProfilerKind.NONE:
        message = "No profiler prompt exists when profiling is disabled."
        raise ValueError(message)
    definition = profiler_definition(kind)
    if definition.requires_domain_torch_support and not supports_torch_profiler:
        message = "The selected domain does not provide Torch profiler support."
        raise ValueError(message)
    return definition


def _run_profiler(
    ctx: LoopContext,
    request: LoopRunRequest,
    progress: RoundProgress,
    profile_focus: str,
    progress_path: Path,
) -> ProfilerSummary | None:
    bundle = request.input_bundle
    round_number = progress.round_number
    domain_definition = resolve_domain(bundle.domain)
    modality = request.modality
    if modality is None and bundle.domain is constants.DomainName.LLM_SERVING:
        modality = "text_generation"
    interface = request.interface
    objective = request.objective or bundle.objective
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
    except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010254 [BLE001]; configured profiler failures become framework warnings so candidate execution can continue.
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


def _run_orchestrator_plan(
    ctx: LoopContext,
    request: LoopRunRequest,
    engine: HypothesisEngine,
    carry: _CarryOver,
    progress: RoundProgress,
) -> OrchestratorPlan:
    bundle = request.input_bundle
    round_number = progress.round_number
    agent_run_state = engine.state
    objective = request.objective or bundle.objective
    modality = request.modality
    if modality is None and bundle.domain is constants.DomainName.LLM_SERVING:
        modality = "text_generation"
    interface = request.interface
    domain_definition = resolve_domain(bundle.domain)
    roadmap_path, progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)
    progress_location = issue_board.display_path(progress_path, ctx.workspace)
    roadmap_location = issue_board.display_path(roadmap_path, ctx.workspace)
    pareto_archive_location = issue_board.display_path(
        issue_board.pareto_archive_path(progress_path), ctx.workspace
    )
    benchmark_contract = BenchmarkContract(
        result_spec=bundle.benchmark_result,
        result_protocol=bundle.benchmark_result_protocol,
    )
    official_eval_every = request.official_eval_every
    provisional_candidates = _provisional_candidates_since_official(agent_run_state.rounds)
    profile_guidance = engine.controller.guidance
    plateau_warning = _detect_plateau(agent_run_state.rounds)
    profiler_summary = carry.profiler_summary
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
        framework_benchmark_enabled=benchmark_contract.declared,
        official_eval_every=official_eval_every,
        provisional_candidates=provisional_candidates,
        official_eval_cadence_due=(provisional_candidates + 1 >= official_eval_every),
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
                pass_criteria=_HEALTH_CHECK_CRITERIA,
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
        message = "Orchestrator hypothesis_updates must name each hypothesis once"
        raise ValueError(message)
    if any(update.hypothesis_id == plan.hypothesis_id for update in plan.hypothesis_updates):
        message = "Orchestrator hypothesis_updates must refer to prior hypotheses"
        raise ValueError(message)
    if state.by_id(plan.hypothesis_id) is not None:
        message = f"hypothesis ID {plan.hypothesis_id!r} was already used"
        raise ValueError(message)
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
    retry: int


@dataclass(frozen=True)
class _RoundAttemptOutcome:
    """Evidence produced by one round's implementer/judge retry cycle."""

    passed: bool
    feedback: str | None
    implementation: ImplementerResponse | None
    single_agent_response: SingleAgentRoundResponse | None = None
    framework_perf_metric: float | None = None
    framework_benchmark: FrameworkBenchmarkOutcome = field(
        default_factory=FrameworkBenchmarkOutcome
    )
    accepted_metrics: dict[str, float] = field(default_factory=dict)
    accepted_evaluation_artifact: str | None = None
    completed_official_evaluation_reason: str | None = None
    attempt_judge: JudgeOutcome = field(
        default_factory=lambda: JudgeSkipped(JudgeSkipReason.NOT_REACHED)
    )
    retry: int = 0
    round_number: int = 0
    retry_limit: int = 0


@dataclass
class _AgentRoundAttempt:
    """Mutable retry-cycle state for one active hypothesis round."""

    engine: HypothesisEngine
    run_state: AgentRunState
    hypothesis: Hypothesis
    state_store: AgentRunStateStore
    feedback: str | None
    framework_revalidation_required: bool
    passed: bool = False
    review_started: bool = False
    implementation_attempt: _ImplementerAttempt | None = None
    implementation: ImplementerResponse | None = None
    single_agent_response: SingleAgentRoundResponse | None = None
    framework_perf_metric: float | None = None
    framework_benchmark: FrameworkBenchmarkOutcome = field(
        default_factory=FrameworkBenchmarkOutcome
    )
    completed_official_evaluation_reason: str | None = None
    attempt_judge: JudgeOutcome = field(
        default_factory=lambda: JudgeSkipped(JudgeSkipReason.NOT_REACHED)
    )
    retry: int = 0
    candidate_ready: bool = False
    official_evaluation_reason: str | None = None


@dataclass(frozen=True)
class _RoundPerformance:
    """Trusted performance fields derived from a completed attempt."""

    profile_skipped: bool
    perf_metric: float | None
    perf_unit: str | None
    perf_provenance: PerfProvenance | None
    accepted_metrics: dict[str, float]
    accepted_evaluation_artifact: str | None


@dataclass(frozen=True)
class _CandidateEvidence:
    """Candidate-side evidence carried from the final attempt or checkpoint."""

    disposition: str
    metrics: dict[str, float]
    evaluation_artifact: str | None
    operating_point: str
    retention_reason: str


def _complete_hypothesis_round(
    engine: HypothesisEngine,
    active_hypothesis: Hypothesis,
    record: RoundRecord,
    request: LoopRunRequest,
    attempt: _RoundAttemptOutcome,
) -> HypothesisEngine:
    """Apply the completed attempt's outcome to hypothesis lifecycle state."""
    next_active_hypothesis: Hypothesis | None = active_hypothesis.clone()
    if (
        request.inner_loop == "multi-agent"
        and attempt.implementation is not None
        and _implementation_keeps_hypothesis_active(
            attempt.implementation,
            continuation_rounds=next_active_hypothesis.continuation_rounds,
        )
    ):
        next_active_hypothesis.feedback = (
            attempt.feedback if record.reviewed and not attempt.passed else None
        )
        next_active_hypothesis.next_step = attempt.implementation.next_step
        next_active_hypothesis.continuation_rounds += 1
    elif attempt.passed:
        next_active_hypothesis = None
    elif (
        record.reviewed
        and next_active_hypothesis.continuation_rounds
        < _MAX_CONTINUATION_ROUNDS_WITHOUT_DESIGN_REVIEW
    ):
        # A rejected review may justify another scoped repair, but it consumes
        # the same bounded ownership lease as an implementer continuation.
        next_active_hypothesis.feedback = attempt.feedback
        next_active_hypothesis.next_step = (
            attempt.implementation.next_step
            if attempt.implementation is not None
            and _implementation_requests_continuation(attempt.implementation)
            else None
        )
        next_active_hypothesis.continuation_rounds += 1
    elif record.reviewed or (
        attempt.implementation is not None
        and not _implementation_keeps_hypothesis_active(
            attempt.implementation,
            continuation_rounds=next_active_hypothesis.continuation_rounds,
        )
    ):
        next_active_hypothesis = None
    else:
        next_active_hypothesis.feedback = None
        next_active_hypothesis.next_step = (
            attempt.implementation.next_step if attempt.implementation is not None else None
        )
    profile_outcome = (
        ProfileGuidanceOutcome.from_round(
            record.round_number,
            passed=attempt.passed,
            official=record.official_evaluation,
            delta_pct=record.perf_delta_pct,
        )
        if request.loop.value == "profile-guided"
        else None
    )
    return engine.complete_round(
        record,
        next_active=next_active_hypothesis,
        profile_outcome=profile_outcome,
    )


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


def _run_implementer(
    ctx: LoopContext,
    request: LoopRunRequest,
    hypothesis: Hypothesis,
    engine: HypothesisEngine,
    progress: RoundProgress,
) -> _ImplementerAttempt:
    state = engine.state
    retry = issue_board.next_implementer_attempt(
        issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1],
        progress.round_number,
    )
    bundle = request.input_bundle
    round_number = progress.round_number
    objective = request.objective or bundle.objective
    modality = request.modality
    if modality is None and bundle.domain is constants.DomainName.LLM_SERVING:
        modality = "text_generation"
    interface = request.interface
    domain_definition = resolve_domain(bundle.domain)
    progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1]
    progress_location = issue_board.display_path(progress_path, ctx.workspace)
    pareto_archive_location = issue_board.display_path(
        issue_board.pareto_archive_path(progress_path), ctx.workspace
    )
    benchmark_contract = BenchmarkContract(
        result_spec=bundle.benchmark_result,
        result_protocol=bundle.benchmark_result_protocol,
        timeout_seconds=framework_command_timeout(ctx, bundle.manifest.benchmark.timeout_seconds),
    )
    guidance = engine.controller.guidance
    official_evaluation_reason = _official_evaluation_reason(
        records=state.rounds,
        progress=progress,
        official_eval_every=request.official_eval_every,
        requested=hypothesis.plan.request_official_evaluation,
        candidate_ready=True,
    )
    if (
        request.loop.value == "profile-guided"
        and guidance.active_component
        and official_evaluation_reason is None
    ):
        official_evaluation_reason = "profile-guided component measurement"
    framework_benchmark_enabled = benchmark_contract.declared
    official_evaluation_due = official_evaluation_reason is not None
    prior_attempt_artifact_locations = tuple(
        issue_board.display_path(path, ctx.workspace)
        for path in issue_board.implementer_artifact_paths(progress_path, round_number)
    )
    plan = hypothesis.plan
    continuation_step = hypothesis.next_step
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
        feedback=hypothesis.feedback,
        continuation_step=continuation_step,
        framework_revert_applied=hypothesis.revert_applied,
        framework_revert_round=hypothesis.parent_round,
        framework_revert_commit=hypothesis.revert_commit,
        gate_revalidation_pending=hypothesis.gate_revalidation_pending,
        gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
        gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
        gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        profile_execution=ctx.run_environment_view.profile_execution,
        framework_benchmark_enabled=framework_benchmark_enabled,
        official_evaluation_due=official_evaluation_due,
        official_evaluation_reason=official_evaluation_reason,
        recommended_skills=resolved_skills,
        prior_attempt_artifact_locations=prior_attempt_artifact_locations,
        **guidance.implementer_prompt_context(),
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
        retry=retry,
    )


def _run_judge(
    ctx: LoopContext,
    request: LoopRunRequest,
    engine: HypothesisEngine,
    progress: RoundProgress,
    attempt: _ImplementerAttempt,
) -> JudgeResponse:
    state = engine.state
    hypothesis = state.active_hypothesis
    if hypothesis is None:
        message = "cannot run a judge without an active hypothesis"
        raise RuntimeError(message)
    implementation = attempt.response
    round_number = progress.round_number
    retry = attempt.retry
    bundle = request.input_bundle
    objective = request.objective or bundle.objective
    modality = request.modality
    interface = request.interface
    domain_definition = resolve_domain(bundle.domain)
    progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1]
    progress_location = issue_board.display_path(progress_path, ctx.workspace)
    pareto_archive_location = issue_board.display_path(
        issue_board.pareto_archive_path(progress_path), ctx.workspace
    )
    benchmark_contract = BenchmarkContract(
        result_spec=bundle.benchmark_result,
        result_protocol=bundle.benchmark_result_protocol,
    )
    official_evaluation_reason = _official_evaluation_reason(
        records=state.rounds,
        progress=progress,
        official_eval_every=request.official_eval_every,
        requested=hypothesis.plan.request_official_evaluation,
        candidate_ready=True,
    )
    if (
        request.loop.value == "profile-guided"
        and engine.controller.guidance.active_component
        and official_evaluation_reason is None
    ):
        official_evaluation_reason = "profile-guided component measurement"
    pareto_archive_conflict = _pareto_archive_conflict(
        candidate_disposition=implementation.candidate_disposition,
        candidate_metrics=dict(implementation.candidate_metrics),
        records=state.rounds,
        space=state.metrics,
    )
    plan = hypothesis.plan
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
        gate_revalidation_pending=hypothesis.gate_revalidation_pending,
        gate_approved_perf_metric=hypothesis.gate_approved_perf_metric,
        gate_approved_perf_unit=hypothesis.gate_approved_perf_unit,
        gate_approved_metrics=dict(hypothesis.gate_approved_metrics),
        gate_approved_evaluation_artifact=hypothesis.gate_approved_evaluation_artifact,
        progress_location=progress_location,
        pareto_archive_location=pareto_archive_location,
        validation_location=validation_location,
        validation_recipe_contract_location=validation_recipe_contract_location,
        framework_revert_applied=hypothesis.revert_applied,
        framework_revert_round=hypothesis.parent_round,
        framework_revert_commit=hypothesis.revert_commit,
        framework_benchmark_enabled=benchmark_contract.declared,
        official_evaluation_due=(official_evaluation_reason is not None),
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


def _run_single_agent_round(
    ctx: LoopContext,
    request: LoopRunRequest,
    hypothesis: Hypothesis,
    engine: HypothesisEngine,
    progress: RoundProgress,
) -> SingleAgentRoundResponse:
    """Invoke one agent that plays implementer + judge + profiler.

    Used when ``--inner-loop=single-agent``. The same backend that the
    multi-agent loop hands to the implementer is used here — it has
    workspace write access plus shell access for benchmarks/profiling.
    """
    state = engine.state
    plan = hypothesis.plan
    round_number = progress.round_number
    progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1]
    retry = issue_board.next_implementer_attempt(progress_path, round_number)
    progress_location = issue_board.display_path(progress_path, ctx.workspace)
    pareto_archive_location = issue_board.display_path(
        issue_board.pareto_archive_path(progress_path), ctx.workspace
    )
    bundle = request.input_bundle
    objective = request.objective or bundle.objective
    modality = request.modality
    if modality is None and bundle.domain is constants.DomainName.LLM_SERVING:
        modality = "text_generation"
    interface = request.interface
    domain_definition = resolve_domain(bundle.domain)
    profile_focus = "general latency hotspots on /v1/completions"
    benchmark_contract = BenchmarkContract(
        result_spec=bundle.benchmark_result,
        result_protocol=bundle.benchmark_result_protocol,
    )
    official_evaluation_reason = _official_evaluation_reason(
        records=state.rounds,
        progress=progress,
        official_eval_every=request.official_eval_every,
        requested=plan.request_official_evaluation,
        candidate_ready=True,
    )
    if (
        request.loop.value == "profile-guided"
        and engine.controller.guidance.active_component
        and official_evaluation_reason is None
    ):
        official_evaluation_reason = "profile-guided component measurement"
    official_evaluation_due = official_evaluation_reason is not None
    framework_benchmark_enabled = benchmark_contract.declared
    pareto_records = state.rounds
    space = state.metrics
    feedback = hypothesis.feedback
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
            message = f"validation input must not be a symlink: {relative}"
            raise ValueError(message)
        path = unresolved.resolve()
        if not path.is_relative_to(workspace_root):
            message = f"validation input escapes workspace: {relative}"
            raise ValueError(message)
        if not path.exists():
            message = f"validation input does not exist: {relative}"
            raise ValueError(message)
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0dir\0" if path.is_dir() else b"\0file\0")
        entries = [path]
        if path.is_dir():
            entries = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
        for entry in entries:
            if entry.is_symlink():
                message = f"validation input must not be a symlink: {relative}"
                raise ValueError(message)
            total_files += 1
            total_bytes += entry.stat().st_size
            if (
                total_files > _MAX_VALIDATION_INPUT_FILES
                or total_bytes > _MAX_VALIDATION_INPUT_BYTES
            ):
                message = "validation inputs exceed the 4096-file/256-MiB reuse-hash limit"
                raise ValueError(message)
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
        message = "validation recipe artifact escapes the workspace"
        raise ValueError(message)
    if not path.is_file():
        message = f"validation recipe artifact does not exist: {artifact}"
        raise ValueError(message)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        message = f"validation recipe artifact is not valid JSON: {exc}"
        raise ValueError(message) from exc
    try:
        return ValidationRecipeArtifact.model_validate(payload).recipes
    except (TypeError, ValueError) as exc:
        message = f"validation recipe artifact does not match version 1: {exc}"
        raise ValueError(message) from exc


def _execute_framework_validation_recipe(
    ctx: LoopContext,
    recipe: ValidationRecipe,
    progress_path: Path,
    round_number: int,
) -> tuple[FrameworkValidationResult, bool]:
    """Run or reuse one local validation recipe and report workspace mutation."""
    try:
        input_digest = _validation_input_digest(ctx.workspace, recipe)
    except (OSError, ValueError) as exc:
        return (
            FrameworkValidationResult(
                recipe=recipe,
                input_digest="",
                passed=False,
                error=str(exc),
            ),
            False,
        )

    reused = _reusable_validation_result(progress_path, recipe, input_digest)
    emit_gate_started(
        GateKind.VALIDATION,
        recipe=recipe.name,
        command=recipe.command,
        round_label=f"round-{round_number}",
    )
    if reused is not None:
        emit_gate_finished(
            GateFinishedData(
                gate=GateKind.VALIDATION,
                recipe=recipe.name,
                reused=True,
            ),
            passed=True,
            round_label=f"round-{round_number}",
        )
        return reused, False

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
    except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010255 [BLE001]; command and backend failures become a failed validation result for the candidate.
        result = FrameworkValidationResult(
            recipe=recipe,
            input_digest=input_digest,
            passed=False,
            error=f"command could not be executed: {exc}",
        )

    changes = ctx.git.pending_changes()
    if changes:
        shown = ", ".join(changes[:_ROLE_CHANGE_DISPLAY_LIMIT])
        suffix = (
            ""
            if len(changes) <= _ROLE_CHANGE_DISPLAY_LIMIT
            else f", ... (+{len(changes) - _ROLE_CHANGE_DISPLAY_LIMIT} more)"
        )
        result = result.model_copy(
            update={
                "passed": False,
                "error": f"validation command mutated the workspace: {shown}{suffix}",
            }
        )
    failure_detail = None if result.passed else (result.error or result.output or "unknown failure")
    emit_gate_finished(
        GateFinishedData(
            gate=GateKind.VALIDATION,
            recipe=recipe.name,
            output_tail=(None if failure_detail is None else failure_detail[-GATE_LOG_TAIL_CHARS:]),
        ),
        passed=result.passed,
        round_label=f"round-{round_number}",
    )
    return result, bool(changes)


def _run_framework_validation_gate(
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
        result, mutated = _execute_framework_validation_recipe(
            ctx, recipe, progress_path, round_number
        )
        results.append(result)
        restore_required |= mutated
        if not result.passed:
            break

    if restore_required and not ctx.git.checkout_tree(checkpoint, clean=True):
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
        return None
    detail = failed.error or failed.output or "unknown failure"
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


def _candidate_revision(ctx: LoopContext) -> str | None:
    revision = ctx.git.current_sha()
    return revision if isinstance(revision, str) else None


def _run_framework_accuracy_gate(
    ctx: LoopContext,
    request: LoopRunRequest,
    progress: RoundProgress,
    retry: int,
    progress_path: Path,
) -> str | None:
    """Run the immutable manifest accuracy command after an agent reports PASS."""
    round_number = progress.round_number
    bundle = request.input_bundle
    benchmark_contract = BenchmarkContract(
        result_spec=bundle.benchmark_result,
        result_protocol=bundle.benchmark_result_protocol,
        timeout_seconds=framework_command_timeout(ctx, bundle.manifest.benchmark.timeout_seconds),
    )
    command = ctx.judge_accuracy_command
    execution_command = None
    if command:
        execution_command = _with_candidate_revision(
            command,
            _candidate_revision(ctx),
            release_deployment_env_var=(
                _deployment_release_env_var(ctx)
                if not benchmark_contract.declared or not ctx.judge_benchmark_command
                else None
            ),
        )
    result = run_accuracy_gate(
        ctx,
        process_id=f"accuracy-{round_number}-{retry}",
        timeout_seconds=framework_command_timeout(ctx, bundle.manifest.accuracy.timeout_seconds),
        execution_command=execution_command,
        round_label=f"round-{round_number}",
    )
    if result.passed and not result.executed:
        return None

    issue_board.append_framework_accuracy_gate(
        progress_path,
        round_number,
        retry,
        result=replace(result, output=result.output[-GATE_RECORD_TAIL_CHARS:]),
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-accuracy")
    return result.feedback


def _run_framework_benchmark(
    ctx: LoopContext,
    request: LoopRunRequest,
    progress: RoundProgress,
    retry: int,
    progress_path: Path,
) -> FrameworkBenchmarkOutcome:
    """Run the shared benchmark gate and record its agent-loop bookkeeping.

    The gate itself (result recovery, parsing, the collision-proof result
    path, and the typed gate events) lives in :mod:`vibesys.loops.gates`;
    this wrapper owns what is agent-loop specific: progress notes and
    workspace snapshots.
    """
    round_number = progress.round_number
    bundle = request.input_bundle
    benchmark_contract = BenchmarkContract(
        result_spec=bundle.benchmark_result,
        result_protocol=bundle.benchmark_result_protocol,
        timeout_seconds=framework_command_timeout(ctx, bundle.manifest.benchmark.timeout_seconds),
    )
    execution_base = None
    if ctx.judge_benchmark_command:
        execution_base = _with_candidate_revision(
            ctx.judge_benchmark_command,
            _candidate_revision(ctx),
            release_deployment_env_var=_deployment_release_env_var(ctx),
        )
    result = run_benchmark_gate(
        ctx,
        contract=benchmark_contract,
        space=request.metrics,
        process_id=f"benchmark-{round_number}-{retry}",
        output_slug=f"{round_number}-{retry}",
        execution_base=execution_base,
        round_label=f"round-{round_number}",
    )
    if not result.executed:
        return result.outcome

    issue_board.append_framework_benchmark(
        progress_path,
        round_number,
        retry,
        result=replace(result, output=result.output[-GATE_RECORD_TAIL_CHARS:]),
        metric_name=(
            result.outcome.metric_name
            or (
                benchmark_contract.result_spec.metric
                if benchmark_contract.result_spec is not None
                else None
            )
        ),
    )
    ctx.snapshot_workspace(f"round-{round_number}-retry-{retry}-framework-benchmark")
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
    try:
        volumes = reconcile_model_requests(ctx.workspace, log=ctx.lprint)
    except ModelRequestError as exc:
        ctx.lprint(f"[model-request] rejected: {exc}")
        return f"Model-weight request could not be satisfied: {exc}"
    if volumes:
        ctx.lprint(f"[model-request] staged {len(volumes)} model volume(s): " + ", ".join(volumes))
    return None


def _run_framework_gates(
    ctx: LoopContext,
    request: LoopRunRequest,
    progress: RoundProgress,
    retry: int,
    hypothesis: Hypothesis,
) -> tuple[str | None, FrameworkBenchmarkOutcome, bool, str | None]:
    """Run the framework-owned gates, returning the first failure's feedback.

    The benchmark outcome is always returned so a passing protocol-path run can
    carry its complete metric row to the round record; it is empty whenever the
    benchmark did not run.
    """
    round_number = progress.round_number
    progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1]
    candidate_revision = _candidate_revision(ctx)
    reuse_accuracy_pass = bool(
        hypothesis.gate_revalidation_pending
        and candidate_revision is not None
        and hypothesis.gate_candidate_commit == candidate_revision
        and hypothesis.gate_accuracy_passed
    )
    if ctx.agent_client.backend_name == "stub":
        return None, FrameworkBenchmarkOutcome(), False, candidate_revision
    resource_feedback = _reconcile_model_requests(ctx)
    if resource_feedback is not None:
        return resource_feedback, FrameworkBenchmarkOutcome(), False, candidate_revision
    if reuse_accuracy_pass:
        feedback = None
        issue_board.append_framework_accuracy_gate(
            progress_path,
            round_number,
            retry,
            result=AccuracyGateResult(
                command=ctx.judge_accuracy_command,
                passed=True,
                output=(
                    "Reused the prior framework-owned PASS for this exact candidate "
                    "commit; a later gate, not accuracy, caused the retry."
                ),
                feedback=None,
                executed=False,
            ),
        )
        emit_gate_started(
            GateKind.ACCURACY,
            command=ctx.judge_accuracy_command or None,
            round_label=f"round-{round_number}",
        )
        emit_gate_finished(
            GateFinishedData(gate=GateKind.ACCURACY, reused=True),
            passed=True,
            round_label=f"round-{round_number}",
        )
    else:
        feedback = _run_framework_accuracy_gate(ctx, request, progress, retry, progress_path)
    if feedback is not None:
        return feedback, FrameworkBenchmarkOutcome(), False, candidate_revision
    benchmark = _run_framework_benchmark(ctx, request, progress, retry, progress_path)
    return benchmark.feedback, benchmark, True, candidate_revision


def _validate_agent_request(request: LoopRunRequest) -> DomainDefinition:
    """Validate agent-loop options and resolve the requested domain."""
    if request.inner_loop not in _INNER_LOOPS:
        message = (
            f"Unknown inner_loop {request.inner_loop!r}; choose from {', '.join(_INNER_LOOPS)}"
        )
        raise ValueError(message)
    if request.max_retries_per_round < 1:
        message = f"max_retries_per_round must be >= 1, got {request.max_retries_per_round}"
        raise ValueError(message)
    if request.judge_every < 1:
        message = f"judge_every must be >= 1, got {request.judge_every}"
        raise ValueError(message)
    if request.official_eval_every < 1:
        message = f"official_eval_every must be >= 1, got {request.official_eval_every}"
        raise ValueError(message)
    if request.memory_layout not in issue_board.MEMORY_LAYOUTS:
        message = (
            f"Unknown memory_layout {request.memory_layout!r}; "
            f"choose from {', '.join(issue_board.MEMORY_LAYOUTS)}"
        )
        raise ValueError(message)
    if request.interface not in _INTERFACES:
        message = f"Unknown interface {request.interface!r}; choose from {', '.join(_INTERFACES)}"
        raise ValueError(message)
    if (
        request.loop.value == "profile-guided"
        and request.input_bundle.manifest.profile_guided is None
    ):
        message = "profile-guided runs require profile guidance settings"
        raise ValueError(message)
    return resolve_domain(request.input_bundle.domain)


def _handle_agent_round_limit(
    ctx: LoopContext,
    request: LoopRunRequest,
    history: RoundHistory,
    progress_path: Path,
    round_number: int,
) -> bool | None:
    """Finalize a completed resumed run or reject an exhausted total budget."""
    max_rounds = request.max_rounds if request.max_rounds is not None else 24
    if round_number <= max_rounds:
        return None
    try:
        if request.resume is not None and history.records:
            ctx.lprint(
                f"This run already completed {len(history.records)} rounds; "
                "finalizing its retained result."
            )
            _finalize_agent_run(
                ctx,
                records=history.records,
                space=request.metrics,
                progress_path=progress_path,
            )
            return True
        message = (
            f"This run has completed {round_number - 1} rounds; max_rounds={max_rounds} "
            "is a total limit. Increase --max-rounds to continue."
        )
        raise ValueError(message)
    finally:
        ctx.close()


def _plan_agent_round(
    ctx: LoopContext,
    request: LoopRunRequest,
    engine: HypothesisEngine,
    carry: _CarryOver,
    progress: RoundProgress,
) -> tuple[HypothesisEngine, OrchestratorPlan]:
    """Select a new hypothesis or continue the active one."""
    round_number = progress.round_number
    bundle = request.input_bundle
    active_hypothesis = engine.state.active_hypothesis
    progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1]
    if active_hypothesis is None:
        profile_guided = (
            bundle.manifest.profile_guided if request.loop.value == "profile-guided" else None
        )
        if profile_guided is not None:
            engine = engine.prepare_profile(ctx, profile_guided, round_number=round_number)
            persist_agent_run_state(
                ctx,
                AgentRunStateStore(ctx.state.portable(RunStateNamespace.AGENT)),
                engine.state,
                label=f"profile-guided: prepare round {round_number}",
            )
        profiler_summary: ProfilerSummary | None = None
        if request.inner_loop == "multi-agent":
            pre_decision = _run_pre_round_decision(
                ctx,
                request=request,
                progress=progress,
                carry=carry,
                has_history=not _is_fresh_cold_start(round_number, engine.state.rounds),
            )
            if pre_decision.need_profile and ctx.profiler_kind is not ProfilerKind.NONE:
                profiler_summary = _run_profiler(
                    ctx,
                    request,
                    progress,
                    pre_decision.profile_focus or "general steady-state benchmark hotspots",
                    progress_path=progress_path,
                )
        elif carry.single_agent_response is not None:
            profiler_summary = _profiler_summary_from_single_agent(carry.single_agent_response)
        carry.profiler_summary = profiler_summary
        plan = _run_orchestrator_plan(ctx, request, engine, carry, progress)
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
                for record in reversed(engine.state.rounds)
                if record.round_number == parent_round
            ),
            None,
        )
        engine = engine.start(
            plan,
            started_round=round_number,
            parent_round=parent_round,
            parent_commit=(
                parent_record.commit
                if parent_record is not None and parent_record.commit is not None
                else ctx.git.current_sha()
            ),
        )
        active_hypothesis = _require_active_hypothesis(engine)
        plan = active_hypothesis.plan
        persist_agent_run_state(
            ctx,
            AgentRunStateStore(ctx.state.portable(RunStateNamespace.AGENT)),
            engine.state,
            label=f"agent: start hypothesis {plan.hypothesis_id}",
        )
        publish_experiments_changed(
            ctx, engine.state, "active_hypothesis_changed", plan_changed_keys(plan)
        )
        return engine, plan

    plan = active_hypothesis.plan
    issue_board.append_hypothesis_continuation(
        progress_path,
        round_number,
        plan=plan,
        started_round=active_hypothesis.started_round,
        continuation_step=active_hypothesis.next_step or plan.task,
    )
    ctx.lprint(f"[hypothesis] continuing {plan.hypothesis_id}; designer invocation skipped")
    return engine, plan


def _apply_agent_rollback(
    ctx: LoopContext,
    request: LoopRunRequest,
    plan: OrchestratorPlan,
    history: RoundHistory,
    engine: HypothesisEngine,
) -> HypothesisEngine:
    """Restore an explicitly selected parent checkpoint for a new hypothesis."""
    hypothesis = _require_active_hypothesis(engine)
    if plan.revert_to_round is None or hypothesis.revert_applied:
        return engine
    target = next(
        (record for record in history.records if record.round_number == plan.revert_to_round),
        None,
    )
    if target is None or target.commit is None:
        output_sink().framework_warning(
            f"cannot revert: no commit recorded for round {plan.revert_to_round}",
            source=FrameworkSource.LOOP,
        )
        return engine
    rollback_commit, failed_child_round = history.resolve_rollback_commit(
        target, _FAILED_HYPOTHESIS_OUTCOMES
    )
    if rollback_commit is None:
        message = f"rollback target round {target.round_number} has no commit"
        raise RuntimeError(message)
    roadmap_path, progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)
    memory_paths = tuple(
        str(path.relative_to(ctx.workspace))
        for path in (roadmap_path, progress_path, issue_board.pareto_archive_path(progress_path))
    )
    if not ctx.git.checkout_tree(rollback_commit, clean=True, preserve_paths=memory_paths):
        output_sink().framework_warning(
            "rollback was not applied; will retry round "
            f"{plan.revert_to_round} on the next continuation",
            source=FrameworkSource.LOOP,
        )
        return engine
    if failed_child_round is None:
        ctx.lprint(f"Reverted workspace to round {plan.revert_to_round} ({rollback_commit[:8]}).")
    else:
        ctx.lprint(
            "Reverted workspace to the pre-hypothesis parent of "
            f"failed round {failed_child_round} ({rollback_commit[:8]}), "
            f"based on parent round {plan.revert_to_round}."
        )
    hypothesis.revert_applied = True
    hypothesis.revert_commit = rollback_commit
    hypothesis.parent_commit = rollback_commit
    state = persist_active_hypothesis(
        ctx,
        AgentRunStateStore(ctx.state.portable(RunStateNamespace.AGENT)),
        engine.state,
        hypothesis,
        label=f"agent: set hypothesis {plan.hypothesis_id} parent",
    )
    return engine.replace_state(state)


def _build_completed_round_record(
    ctx: LoopContext,
    request: LoopRunRequest,
    engine: HypothesisEngine,
    history: RoundHistory,
    attempt: _RoundAttemptOutcome,
) -> RoundRecord:
    """Construct the trusted durable record for a completed retry cycle."""
    hypothesis = _require_active_hypothesis(engine)
    performance = _round_performance(request, hypothesis, attempt)
    candidate = _candidate_evidence(attempt, hypothesis)
    implementation = attempt.implementation
    response = attempt.single_agent_response
    reviewed = isinstance(attempt.attempt_judge, JudgeReviewed)
    declared_outcome = (
        implementation.hypothesis_outcome
        if implementation is not None
        else HypothesisOutcome.NOMINATED
        if response is not None
        else None
    )
    bundle = request.input_bundle
    framework_benchmark_configured = (
        bundle.benchmark_result is not None or bundle.benchmark_result_protocol is not None
    )
    official_evaluation = (
        attempt.passed
        and attempt.completed_official_evaluation_reason is not None
        and ctx.agent_client.backend_name != "stub"
        and (bool(ctx.judge_accuracy_command) or framework_benchmark_configured)
    )
    primary_objective = request.metrics.objectives[0] if request.metrics.objectives else None
    metric_name = (
        attempt.framework_benchmark.metric_name
        or (primary_objective.name if primary_objective is not None else None)
        or performance.perf_unit
    )
    metric_direction = attempt.framework_benchmark.metric_direction or (
        primary_objective.direction if primary_objective is not None else None
    )
    accepted_metrics = dict(performance.accepted_metrics)
    if attempt.framework_benchmark.row is not None and performance.perf_metric is not None:
        accepted_metrics = dict(attempt.framework_benchmark.row)
    if not accepted_metrics and performance.perf_metric is not None and performance.perf_unit:
        accepted_metrics = {performance.perf_unit: performance.perf_metric}
    official_metric = (
        accepted_metrics.get(metric_name) if metric_name is not None else performance.perf_metric
    )
    if official_metric is None and not accepted_metrics:
        official_metric = performance.perf_metric
    parent = metric_baseline(
        parent_round=hypothesis.parent_round,
        parent_commit=hypothesis.parent_commit,
        metric=metric_name,
        rounds=history.records,
    )
    baseline_metric = record_metric_value(parent, metric_name) if parent is not None else None
    framework_provenance = trusted_perf_provenance(performance.perf_provenance)
    official_reading = (
        Measurement(metric=metric_name, value=official_metric, direction=metric_direction)
        if metric_name is not None and official_metric is not None
        else None
    )
    comparison = (
        engine.state.metrics.compare(
            official_reading,
            Measurement(metric=metric_name, value=baseline_metric, direction=metric_direction)
            if metric_name is not None and baseline_metric is not None
            else None,
        )
        if official_evaluation and official_metric is not None and framework_provenance
        else None
    )
    resolution = resolve_hypothesis_outcome(
        ResolutionEvidence(
            declared=declared_outcome,
            passed=attempt.passed,
            reviewed=reviewed,
            comparison=comparison,
        )
    )
    disposition = CandidateDisposition(candidate.disposition)
    if not reviewed:
        candidate_retained = _provisional_candidate_retained(disposition)
    elif not attempt.passed:
        candidate_retained = False
    elif (
        official_evaluation
        and framework_provenance
        and request.metrics.objectives
        and accepted_metrics
    ):
        candidate_retained = not _pareto_archive_dominators(
            accepted_metrics, history.records, engine.state.metrics
        )
    elif official_evaluation and framework_provenance:
        prior_readings = [
            Measurement(metric=metric_name, value=value, direction=metric_direction)
            for record in history.records
            if metric_name is not None
            and record.official_evaluation
            and trusted_perf_provenance(record.perf_provenance)
            and (value := record_metric_value(record, metric_name)) is not None
        ]
        candidate_retained = scalar_candidate_retained(
            engine.state.metrics.compare_to_best(official_reading, prior_readings)
        )
    else:
        candidate_retained = _provisional_candidate_retained(disposition)
    delta = None
    if framework_provenance and official_metric is not None and baseline_metric not in (None, 0):
        delta = (official_metric - baseline_metric) / abs(baseline_metric) * 100
    return RoundRecord(
        round_number=attempt.round_number,
        commit=ctx.git.current_sha(),
        perf_metric=performance.perf_metric,
        perf_unit=performance.perf_unit,
        passed=attempt.passed,
        profile_skipped=performance.profile_skipped,
        hypothesis_id=hypothesis.plan.hypothesis_id,
        hypothesis_declared_outcome=declared_outcome.value if declared_outcome else None,
        judge_verdict=recorded_judge_verdict(attempt.attempt_judge),
        hypothesis_outcome=resolution.value
        if resolution is not None
        else (declared_outcome.value if declared_outcome else None),
        hypothesis_claim=hypothesis.plan.hypothesis or None,
        hypothesis_task=hypothesis.plan.task or None,
        hypothesis_parent_round=hypothesis.parent_round,
        hypothesis_parent_commit=hypothesis.parent_commit,
        metrics=accepted_metrics,
        evaluation_artifact=performance.accepted_evaluation_artifact,
        official_evaluation=official_evaluation,
        official_evaluation_reason=(
            attempt.completed_official_evaluation_reason if official_evaluation else None
        ),
        candidate_disposition=candidate.disposition,
        candidate_metrics=candidate.metrics,
        candidate_evaluation_artifact=candidate.evaluation_artifact,
        candidate_operating_point=candidate.operating_point,
        candidate_retention_reason=candidate.retention_reason,
        candidate_retained=candidate_retained,
        perf_direction=metric_direction,
        perf_baseline_round=parent.round_number if parent is not None else None,
        perf_baseline_commit=parent.commit if parent is not None else None,
        perf_baseline_metric=baseline_metric,
        perf_delta_pct=delta,
        perf_comparison=comparison,
        perf_provenance=performance.perf_provenance,
        implementer_driver=ctx.agent_client.driver_name,
        implementer_provider=ctx.agent_client.provider,
        implementer_model=ctx.agent_client.model_for_kind("implementer"),
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _persist_agent_hypothesis_checkpoint(
    ctx: LoopContext,
    state_store: AgentRunStateStore,
    state: AgentRunState,
    hypothesis: Hypothesis,
    hypothesis_id: str,
) -> AgentRunState:
    """Persist one approved or feedback-bearing hypothesis checkpoint."""
    return persist_active_hypothesis(
        ctx,
        state_store,
        state,
        hypothesis,
        label=f"agent: checkpoint hypothesis {hypothesis_id}",
    )


def _run_agent_framework_gates(
    ctx: LoopContext,
    request: LoopRunRequest,
    hypothesis: Hypothesis,
    progress: RoundProgress,
    retry: int,
) -> tuple[str | None, FrameworkBenchmarkOutcome, bool, str | None]:
    """Run framework gates for the exact candidate currently under review."""
    return _run_framework_gates(ctx, request, progress, retry, hypothesis)


def _persist_round_attempt_hypothesis(
    ctx: LoopContext,
    attempt: _AgentRoundAttempt,
) -> None:
    attempt.run_state = _persist_agent_hypothesis_checkpoint(
        ctx,
        attempt.state_store,
        attempt.run_state,
        attempt.hypothesis,
        attempt.hypothesis.plan.hypothesis_id,
    )


def _run_official_candidate_gates(
    ctx: LoopContext,
    request: LoopRunRequest,
    round_history: RoundHistory,
    round_progress: RoundProgress,
    attempt: _AgentRoundAttempt,
) -> bool:
    """Run requested official gates; return whether another retry is needed."""
    progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1]
    issue_board.append_official_evaluation_decision(
        progress_path,
        round_progress.round_number,
        attempt.retry,
        run=True,
        reason=attempt.official_evaluation_reason or "requested",
        official_eval_every=request.official_eval_every,
        provisional_candidates=_provisional_candidates_since_official(round_history.records),
    )
    (
        gate_feedback,
        attempt.framework_benchmark,
        accuracy_passed,
        candidate_commit,
    ) = _run_agent_framework_gates(ctx, request, attempt.hypothesis, round_progress, attempt.retry)
    attempt.framework_perf_metric = attempt.framework_benchmark.metric_value
    if gate_feedback is None:
        attempt.passed = True
        attempt.completed_official_evaluation_reason = attempt.official_evaluation_reason
        return False

    attempt.feedback = gate_feedback
    attempt.framework_revalidation_required = request.inner_loop == "multi-agent"
    attempt.hypothesis.gate_revalidation_pending = True
    attempt.hypothesis.gate_candidate_commit = candidate_commit
    attempt.hypothesis.gate_accuracy_passed = accuracy_passed
    attempt.hypothesis.feedback = gate_feedback
    _persist_round_attempt_hypothesis(ctx, attempt)
    return True


def _finish_multi_agent_pass(
    ctx: LoopContext,
    request: LoopRunRequest,
    round_history: RoundHistory,
    round_progress: RoundProgress,
    attempt: _AgentRoundAttempt,
) -> bool:
    """Apply audited candidate evidence and its optional official evaluation."""
    implementation = attempt.implementation
    if implementation is None:
        message = "passed implementer review has no response"
        raise RuntimeError(message)
    progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1]
    validation_feedback = _run_framework_validation_gate(
        ctx,
        recipe_artifact=implementation.validation_recipe_artifact,
        round_number=round_progress.round_number,
        retry=attempt.retry,
        progress_path=progress_path,
    )
    if validation_feedback is not None:
        attempt.feedback = validation_feedback
        attempt.hypothesis.feedback = validation_feedback
        _persist_round_attempt_hypothesis(ctx, attempt)
        return True

    if implementation.candidate_disposition is CandidateDisposition.PARETO_FRONTIER:
        attempt.hypothesis.gate_approved_candidate_disposition = (
            implementation.candidate_disposition.value
        )
        attempt.hypothesis.gate_approved_candidate_metrics = dict(implementation.candidate_metrics)
        attempt.hypothesis.gate_approved_candidate_evaluation_artifact = (
            implementation.candidate_evaluation_artifact
        )
        attempt.hypothesis.gate_approved_candidate_operating_point = (
            implementation.candidate_operating_point
        )
        attempt.hypothesis.gate_approved_candidate_retention_reason = (
            implementation.candidate_retention_reason
        )
        _persist_round_attempt_hypothesis(ctx, attempt)

    attempt.candidate_ready = (
        implementation.hypothesis_outcome
        in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
        or implementation.candidate_disposition is CandidateDisposition.PARETO_FRONTIER
    )
    attempt.official_evaluation_reason = _official_evaluation_reason(
        records=round_history.records,
        progress=round_progress,
        official_eval_every=request.official_eval_every,
        requested=attempt.hypothesis.plan.request_official_evaluation,
        candidate_ready=attempt.candidate_ready,
    )
    if attempt.official_evaluation_reason is None:
        if attempt.candidate_ready:
            issue_board.append_official_evaluation_decision(
                progress_path,
                round_progress.round_number,
                attempt.retry,
                run=False,
                reason="cadence_not_due",
                official_eval_every=request.official_eval_every,
                provisional_candidates=_provisional_candidates_since_official(
                    round_history.records
                ),
            )
            ctx.lprint(
                "[official-evaluation] deferred; candidate "
                "retained as a provisional working checkpoint"
            )
        attempt.passed = True
        return False

    if implementation.perf_metric is not None:
        attempt.hypothesis.gate_approved_perf_metric = implementation.perf_metric
        attempt.hypothesis.gate_approved_perf_unit = implementation.perf_unit
        attempt.hypothesis.gate_approved_metrics = dict(implementation.metrics)
        attempt.hypothesis.gate_approved_evaluation_artifact = implementation.evaluation_artifact
        _persist_round_attempt_hypothesis(ctx, attempt)
    return _run_official_candidate_gates(ctx, request, round_history, round_progress, attempt)


def _run_multi_agent_retry(
    ctx: LoopContext,
    request: LoopRunRequest,
    round_history: RoundHistory,
    round_progress: RoundProgress,
    attempt: _AgentRoundAttempt,
) -> bool:
    """Run one implementer/judge attempt; return whether retrying is appropriate."""
    ctx.reselect_gpu()
    implementer_attempt = _run_implementer(
        ctx,
        request,
        attempt.hypothesis,
        attempt.engine,
        round_progress,
    )
    attempt.implementation_attempt = implementer_attempt
    attempt.implementation = implementer_attempt.response
    if implementer_attempt.synthesized:
        attempt.attempt_judge = JudgeSkipped(JudgeSkipReason.UNPARSEABLE_IMPLEMENTATION)
        ctx.lprint(
            f"[implementer] attempt {attempt.retry}/{request.max_retries_per_round} "
            "returned no parseable structured response; the framework synthesized "
            "a fail-closed one. "
            + (
                "Retrying within the same round."
                if attempt.retry < request.max_retries_per_round
                else "Retries are exhausted; completing the round with it."
            )
        )
        return True

    implementation = attempt.implementation
    records = round_history.records
    review_due = _review_due(
        round_number=round_progress.round_number,
        max_rounds=round_progress.total_rounds,
        judge_every=request.judge_every,
        outcome=implementation.hypothesis_outcome,
        candidate_evidence_fresh=_candidate_evidence_is_fresh(implementation, records),
    )
    if attempt.review_started and not _implementation_requests_continuation(implementation):
        review_due = True
    if (
        attempt.review_started
        and round_progress.round_number != round_progress.total_rounds
        and _implementation_requests_continuation(implementation)
        and implementation.candidate_disposition is not CandidateDisposition.PARETO_FRONTIER
        and not attempt.framework_revalidation_required
    ):
        review_due = False
    if not review_due:
        attempt.attempt_judge = JudgeSkipped(JudgeSkipReason.SPARSE_REVIEW_POLICY)
        issue_board.append_judge_skipped(
            issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1],
            round_progress.round_number,
            outcome=implementation.hypothesis_outcome.value,
            judge_every=request.judge_every,
        )
        ctx.lprint("[judge] deferred by sparse-review policy; official gates were not run")
        return False

    attempt.review_started = True
    attempt.framework_revalidation_required = False
    ctx.reselect_gpu()
    verdict = _run_judge(
        ctx,
        request,
        attempt.engine,
        round_progress,
        implementer_attempt,
    )
    attempt.attempt_judge = JudgeReviewed(verdict.verdict)
    if verdict.verdict == Verdict.PASS:
        return _finish_multi_agent_pass(ctx, request, round_history, round_progress, attempt)

    attempt.feedback = verdict.feedback
    attempt.hypothesis.feedback = verdict.feedback
    _persist_round_attempt_hypothesis(ctx, attempt)
    return True


def _run_single_agent_retry(
    ctx: LoopContext,
    request: LoopRunRequest,
    round_history: RoundHistory,
    round_progress: RoundProgress,
    attempt: _AgentRoundAttempt,
) -> bool:
    """Run one single-agent attempt and its official evaluation phase."""
    ctx.reselect_gpu()
    response = _run_single_agent_round(
        ctx,
        request,
        attempt.hypothesis,
        attempt.engine,
        round_progress,
    )
    attempt.single_agent_response = response
    attempt.attempt_judge = JudgeReviewed(response.verdict)
    if response.verdict != Verdict.PASS:
        attempt.feedback = response.feedback
        attempt.hypothesis.feedback = response.feedback
        _persist_round_attempt_hypothesis(ctx, attempt)
        return True

    attempt.candidate_ready = True
    attempt.official_evaluation_reason = _official_evaluation_reason(
        records=round_history.records,
        progress=round_progress,
        official_eval_every=request.official_eval_every,
        requested=attempt.hypothesis.plan.request_official_evaluation,
        candidate_ready=True,
    )
    if attempt.official_evaluation_reason is None:
        issue_board.append_official_evaluation_decision(
            issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1],
            round_progress.round_number,
            attempt.retry,
            run=False,
            reason="cadence_not_due",
            official_eval_every=request.official_eval_every,
            provisional_candidates=_provisional_candidates_since_official(round_history.records),
        )
        ctx.lprint(
            "[official-evaluation] deferred; candidate retained as a provisional working checkpoint"
        )
        attempt.passed = True
        return False
    return _run_official_candidate_gates(ctx, request, round_history, round_progress, attempt)


def _run_agent_attempts(
    ctx: LoopContext,
    request: LoopRunRequest,
    engine: HypothesisEngine,
    round_history: RoundHistory,
    round_progress: RoundProgress,
) -> tuple[HypothesisEngine, _RoundAttemptOutcome]:
    """Run one round's mode-specific retry phases and framework gates."""
    run_state = engine.state
    hypothesis = _require_active_hypothesis(engine)
    state_store = AgentRunStateStore(ctx.state.portable(RunStateNamespace.AGENT))
    attempt = _AgentRoundAttempt(
        engine=engine,
        run_state=run_state,
        hypothesis=hypothesis,
        state_store=state_store,
        feedback=hypothesis.feedback,
        framework_revalidation_required=hypothesis.gate_revalidation_pending,
    )
    progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)[1]
    max_retries = request.max_retries_per_round
    first_retry = issue_board.next_implementer_attempt(progress_path, round_progress.round_number)
    if first_retry > max_retries:
        message = (
            f"Round {round_progress.round_number} already persisted "
            f"{first_retry - 1} implementer attempts, exhausting "
            f"max_retries_per_round={max_retries}; refusing "
            "to overwrite or replay paid work."
        )
        raise RuntimeError(message)
    if first_retry > 1:
        ctx.lprint(
            f"[resume] round {round_progress.round_number} continues at durable "
            f"attempt {first_retry}/{max_retries}"
        )

    for retry in range(first_retry, max_retries + 1):
        if attempt.engine.state is not attempt.run_state:
            attempt.engine = attempt.engine.replace_state(attempt.run_state)
        attempt.hypothesis = _require_active_hypothesis(attempt.engine)
        attempt.retry = retry
        ctx.lprint(f"\n--- attempt {retry}/{max_retries} ---\n")
        # A skipped audit belongs to the current implementation only; it must
        # never inherit an earlier attempt's verdict.
        attempt.attempt_judge = JudgeSkipped(JudgeSkipReason.NOT_REACHED)
        keep_retrying = (
            _run_multi_agent_retry(ctx, request, round_history, round_progress, attempt)
            if request.inner_loop == "multi-agent"
            else _run_single_agent_retry(ctx, request, round_history, round_progress, attempt)
        )
        if not keep_retrying:
            break

    attempt.run_state = update_active_hypothesis(attempt.run_state, attempt.hypothesis)
    attempt.engine = attempt.engine.replace_state(attempt.run_state)
    return attempt.engine, _RoundAttemptOutcome(
        passed=attempt.passed,
        feedback=attempt.feedback,
        implementation=attempt.implementation,
        single_agent_response=attempt.single_agent_response,
        framework_perf_metric=attempt.framework_perf_metric,
        framework_benchmark=attempt.framework_benchmark,
        completed_official_evaluation_reason=attempt.completed_official_evaluation_reason,
        attempt_judge=attempt.attempt_judge,
        retry=attempt.retry,
        round_number=round_progress.round_number,
        retry_limit=max_retries,
    )


def _round_performance(
    request: LoopRunRequest,
    active_hypothesis: Hypothesis,
    attempt: _RoundAttemptOutcome,
) -> _RoundPerformance:
    """Resolve the trusted headline measurement from the final attempt."""
    accepted_metrics = dict(attempt.accepted_metrics)
    accepted_evaluation_artifact = attempt.accepted_evaluation_artifact
    perf_provenance: PerfProvenance | None = None
    single_agent_response = attempt.single_agent_response
    implementation = attempt.implementation
    if request.inner_loop == "single-agent":
        if (
            single_agent_response is not None
            and attempt.framework_perf_metric is not None
            and attempt.completed_official_evaluation_reason is not None
        ):
            single_agent_response.perf_metric = attempt.framework_perf_metric
            single_agent_response.perf_unit = attempt.framework_benchmark.metric_name
            perf_provenance = "framework"
        profile_skipped = single_agent_response is None or (
            single_agent_response.perf_metric is None
        )
        perf_metric = (
            single_agent_response.perf_metric
            if (
                single_agent_response
                and attempt.passed
                and attempt.completed_official_evaluation_reason is not None
            )
            else None
        )
        perf_unit = (
            single_agent_response.perf_unit
            if (
                single_agent_response
                and attempt.passed
                and attempt.completed_official_evaluation_reason is not None
            )
            else None
        )
        if perf_metric is not None and perf_provenance is None:
            # Not overridden by the framework benchmark above, so this
            # headline number is the agent's own report.
            perf_provenance = "implementer"
    else:
        implementation_metric = (
            implementation.perf_metric
            if (
                implementation is not None
                and attempt.passed
                and attempt.completed_official_evaluation_reason is not None
            )
            else None
        )
        if (
            implementation_metric is None
            and attempt.passed
            and implementation is not None
            and implementation.hypothesis_outcome
            in {HypothesisOutcome.SUPPORTED, HypothesisOutcome.NOMINATED}
            and active_hypothesis.gate_revalidation_pending
            and attempt.completed_official_evaluation_reason is not None
        ):
            implementation_metric = active_hypothesis.gate_approved_perf_metric
        profile_skipped = attempt.framework_perf_metric is None and implementation_metric is None
        if (
            attempt.framework_perf_metric is not None
            and attempt.passed
            and attempt.completed_official_evaluation_reason is not None
        ):
            perf_metric = attempt.framework_perf_metric
            perf_unit = attempt.framework_benchmark.metric_name
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
                accepted_evaluation_artifact = active_hypothesis.gate_approved_evaluation_artifact
        else:
            # Profiles and directional probes inform the designer, but only
            # an official checkpoint updates portable round records.
            perf_metric = None
            perf_unit = None
    return _RoundPerformance(
        profile_skipped=profile_skipped,
        perf_metric=perf_metric,
        perf_unit=perf_unit,
        perf_provenance=perf_provenance,
        accepted_metrics=accepted_metrics,
        accepted_evaluation_artifact=accepted_evaluation_artifact,
    )


def _candidate_evidence(
    attempt: _RoundAttemptOutcome,
    active_hypothesis: Hypothesis,
) -> _CandidateEvidence:
    """Select candidate evidence from the final attempt or approved checkpoint."""
    if attempt.implementation is not None:
        implementation = attempt.implementation
        disposition = implementation.candidate_disposition.value
        metrics = dict(implementation.candidate_metrics)
        evaluation_artifact = implementation.candidate_evaluation_artifact
        operating_point = implementation.candidate_operating_point
        retention_reason = implementation.candidate_retention_reason
    elif attempt.single_agent_response is not None:
        response = attempt.single_agent_response
        disposition = response.candidate_disposition.value
        metrics = dict(response.candidate_metrics)
        evaluation_artifact = response.candidate_evaluation_artifact
        operating_point = response.candidate_operating_point
        retention_reason = response.candidate_retention_reason
    else:
        disposition = CandidateDisposition.UNASSESSED.value
        metrics = {}
        evaluation_artifact = None
        operating_point = ""
        retention_reason = ""
    # A framework-gate retry may return no fresh candidate row. Preserve the
    # judge-approved provisional evidence for the unchanged checkpoint.
    if (
        disposition == CandidateDisposition.UNASSESSED.value
        and active_hypothesis.gate_revalidation_pending
        and active_hypothesis.gate_approved_candidate_disposition
        == CandidateDisposition.PARETO_FRONTIER.value
    ):
        disposition = active_hypothesis.gate_approved_candidate_disposition
        metrics = dict(active_hypothesis.gate_approved_candidate_metrics)
        evaluation_artifact = active_hypothesis.gate_approved_candidate_evaluation_artifact
        operating_point = active_hypothesis.gate_approved_candidate_operating_point
        retention_reason = active_hypothesis.gate_approved_candidate_retention_reason
    return _CandidateEvidence(
        disposition=disposition,
        metrics=metrics,
        evaluation_artifact=evaluation_artifact,
        operating_point=operating_point,
        retention_reason=retention_reason,
    )


def _restore_agent_run_state(
    ctx: LoopContext,
    request: LoopRunRequest,
    state_store: AgentRunStateStore,
) -> AgentRunState:
    """Migrate legacy checkpoints and persist the authoritative run state."""
    legacy_records = ctx.state.completed_rounds()
    local_namespace = ctx.state.local(RunStateNamespace.AGENT)
    state = state_store.migrate_legacy(
        rounds=legacy_records,
        local_namespace=local_namespace,
        legacy_space=request.metrics,
    )
    # The launching task defines the run's metric space. Persist it once so
    # resume, retention, and server projections all share the same axes.
    state = adopt_metric_space(state, request.metrics)
    active_hypothesis = state.active_hypothesis
    if active_hypothesis is not None and _backfill_revert_commit(active_hypothesis, state.rounds):
        state = update_active_hypothesis(state, active_hypothesis)
    state_store.save(state)
    state_store.cleanup_legacy_portable([record.round_number for record in legacy_records])
    ctx.state.commit("agent: migrate unified hypothesis state", state_store.namespace)
    ctx.publish_committed_state("agent", state)
    state_store.cleanup_legacy_local(local_namespace)
    return state


def _agent_project_configuration(
    request: LoopRunRequest,
    run_environment: RunEnvironmentSpec,
    modality: str | None,
) -> AgentRunConfiguration:
    """Build the persisted effective configuration for one agent run."""
    bundle = request.input_bundle
    metrics = request.metrics
    benchmark_result = bundle.benchmark_result
    objectives = list(metrics.objectives)
    manifest_axes: dict[str, Literal["max", "min"]] = {
        objective.name: objective.direction for objective in objectives
    }
    if benchmark_result is not None:
        manifest_axes.setdefault(benchmark_result.metric, "max")
    normalized_config = as_config(request.config)
    agent_backend = request.agent_backend
    resolved_agent_backend = (
        "stub"
        if agent_backend == "stub"
        else str(agent_backend or normalized_config.agent.backend or AgentBackend.CLI)
    )
    return AgentRunConfiguration(
        outer_loop="profile-guided" if request.loop.value == "profile-guided" else "agent",
        run_environment=run_environment_record(run_environment),
        inner_loop=request.inner_loop,
        interface=request.interface,
        model=normalized_config.model.name,
        agent_backend=resolved_agent_backend,
        agent_driver=(
            resolve_agent_driver(normalized_config).value
            if resolved_agent_backend == "cli"
            else None
        ),
        cli_provider=(
            request.cli_provider or normalized_config.agent.cli_provider or "codex"
            if agent_backend != "stub"
            else None
        ),
        compute_backend=request.backend.value,
        profiler=request.profiler_kind.value,
        max_rounds=request.max_rounds if request.max_rounds is not None else 24,
        max_retries_per_round=request.max_retries_per_round,
        judge_every=request.judge_every,
        official_eval_every=request.official_eval_every,
        memory_layout=request.memory_layout,
        modality=modality,
        cli_timeout=normalized_config.agent.cli_timeout,
        default_reasoning_effort=normalized_config.thinking.level,
        outer_model=normalized_config.agent.outer.model,
        outer_reasoning_effort=normalized_config.agent.outer.reasoning_effort,
        inner_model=normalized_config.agent.inner.model,
        inner_reasoning_effort=normalized_config.agent.inner.reasoning_effort,
        operator_constraints=request.operator_constraints,
        objectives=tuple(f"{name}:{direction}" for name, direction in manifest_axes.items()),
    )


def _create_agent_run_context(
    request: LoopRunRequest,
    integration: LocalRunIntegration | None,
) -> LoopContext:
    """Validate the request and assemble its configured run context."""
    bundle = request.input_bundle
    existing = request.resume is not None
    exp_name = request.resume.run_id if request.resume is not None else request.exp_name
    if exp_name is None:
        message = "exp_name must be set for a fresh (non-resume) run"
        raise ValueError(message)
    domain_definition = _validate_agent_request(request)
    modality = request.modality
    if modality is None and domain_definition.name is constants.DomainName.LLM_SERVING:
        modality = "text_generation"
    run_environment = request.run_environment or make_run_environment_spec()
    benchmark_contract = BenchmarkContract(
        result_spec=bundle.benchmark_result,
        result_protocol=bundle.benchmark_result_protocol,
    )
    normalized_config = as_config(request.config)
    project_configuration = _agent_project_configuration(request, run_environment, modality)
    return create_run_context(
        config=normalized_config,
        exp_name=exp_name,
        runs_dir=request.runs_dir,
        input_path=str(bundle.root),
        accuracy_command=bundle.accuracy_command_display,
        benchmark_command=bundle.benchmark_command_display,
        task_name=bundle.task_name,
        task_root=bundle.task_root,
        workspace_sources=bundle.workspace_sources,
        evaluator_path=bundle.evaluator_path,
        evaluator_package_root=bundle.evaluator_package_root,
        benchmark_output_argument=benchmark_contract.output_argument,
        objective=request.objective or bundle.objective,
        existing=existing,
        project_configuration=project_configuration,
        trusted_input_baseline=None,
        debug=request.debug,
        profiler_kind=request.profiler_kind,
        profiler_domain=domain_definition.name,
        skills_dirs=request.skills_dirs,
        run_environment=run_environment,
        agent_backend=request.agent_backend,
        cli_provider=request.cli_provider,
        backend=request.backend,
        environment_hooks=domain_definition.environment_hooks,
        remote_repo=request.remote_repo,
        repo_visibility=request.repo_visibility,
        agent_state_model_type=AgentRunState,
        integration=integration,
    )


def _update_round_carry_over(
    carry: _CarryOver,
    active_hypothesis: Hypothesis,
    round_history: RoundHistory,
    attempt: _RoundAttemptOutcome,
    progress_path: Path,
) -> None:
    """Set designer feedback from one durably recorded round."""
    record = round_history.records[-1]
    implementation = attempt.implementation
    if not record.passed and record.reviewed:
        issue_board.append_exhaustion_note(
            progress_path,
            record.round_number,
            attempt.retry_limit,
            attempt.feedback or "",
        )
        carry.exhaustion_info = (
            f"Round {record.round_number} did not pass after "
            f"{attempt.retry_limit} attempts. Last judge feedback: "
            f"{attempt.feedback or '(empty)'}"
        )
        carry.regression_info = None
    elif record.passed:
        carry.exhaustion_info = None
        if (
            attempt.single_agent_response is None
            and implementation is not None
            and not _implementation_keeps_hypothesis_active(
                implementation,
                continuation_rounds=active_hypothesis.continuation_rounds,
            )
            and implementation.hypothesis_outcome is not HypothesisOutcome.NOMINATED
        ):
            carry.regression_info = _terminal_workspace_notice(round_history.records)
        elif record.official_evaluation and record.candidate_retained is False:
            perf_value = record.perf_metric
            perf_unit = record.perf_unit
            carry.regression_info = (
                f"Round {record.round_number}'s official candidate was not retained: "
                f"{perf_value}{(' ' + perf_unit) if perf_unit else ''}. "
                "Use its recorded parent and objective directions when choosing "
                "the next checkpoint."
            )
        else:
            carry.regression_info = None
    else:
        carry.exhaustion_info = None
        carry.regression_info = (
            None
            if _implementation_keeps_hypothesis_active(
                implementation,
                continuation_rounds=active_hypothesis.continuation_rounds,
            )
            else _terminal_workspace_notice(round_history.records)
        )


def _begin_completed_agent_round(
    ctx: LoopContext,
    request: LoopRunRequest,
    engine: HypothesisEngine,
    record: RoundRecord,
    attempt: _RoundAttemptOutcome,
) -> HypothesisEngine:
    """Journal the transition and begin persistence for one completed round."""
    active_hypothesis = engine.state.active_hypothesis
    if active_hypothesis is None:
        message = "hypothesis engine did not retain the current hypothesis"
        raise RuntimeError(message)
    engine = _complete_hypothesis_round(engine, active_hypothesis, record, request, attempt)
    state_store = AgentRunStateStore(ctx.state.portable(RunStateNamespace.AGENT))
    ctx.begin_completed_round(
        record.round_number,
        state_transition=state_store.transition(engine.state),
    )
    return engine


def _persist_completed_agent_round(
    ctx: LoopContext,
    engine: HypothesisEngine,
    record: RoundRecord,
    attempt: _RoundAttemptOutcome,
) -> None:
    """Persist a completed round and publish its lifecycle event."""
    state = engine.state
    ctx.persist_completed_round()
    publish_experiments_changed(ctx, state, "round_persisted", (record.hypothesis_id,))
    ctx.events.emit(
        CoreEventType.ROUND_FINISHED,
        status=(
            EventStatus.COMPLETED if attempt.passed or not record.reviewed else EventStatus.FAILED
        ),
        round_label=f"round-{record.round_number}",
        data=RoundFinishedData(
            attempts=attempt.retry,
            judge_verdict=("pass" if attempt.passed else "fail" if record.reviewed else "skipped"),
            perf_metric=record.perf_metric,
            perf_unit=record.perf_unit,
            profile_skipped=record.profile_skipped,
        ),
    )


def _prepare_agent_round_session(
    ctx: LoopContext,
    request: LoopRunRequest,
    start_round: int | None,
) -> _AgentRoundSession:
    """Prepare the durable history and workspace files for round execution."""
    existing = request.resume is not None
    first_round = (1 if not existing else None) if start_round is None else start_round
    roadmap_path, progress_path = issue_board.resolve_paths(ctx.workspace, request.memory_layout)
    issue_board.ensure_progress_file(progress_path)
    issue_board.ensure_roadmap_file(roadmap_path)
    issue_board.write_validation_recipe_schema(progress_path)
    state_store = AgentRunStateStore(ctx.state.portable(RunStateNamespace.AGENT))
    run_state = _restore_agent_run_state(ctx, request, state_store)
    history = RoundHistory(records=run_state.rounds)
    return _AgentRoundSession(
        run_state=run_state,
        history=history,
        carry=_CarryOver(regression_info=_terminal_workspace_notice(history.records)),
        progress_path=progress_path,
        round_number=(first_round if first_round is not None else len(history.records) + 1),
    )


def _create_agent_hypothesis_engine(
    request: LoopRunRequest,
    run_state: AgentRunState,
) -> HypothesisEngine:
    """Create the controller with profile-guidance settings for this run."""
    profile_guidance = (
        request.input_bundle.manifest.profile_guided
        if request.loop.value == "profile-guided"
        else None
    )
    return HypothesisEngine.create(run_state, config=profile_guidance)


def _require_active_hypothesis(engine: HypothesisEngine) -> Hypothesis:
    """Return the engine's active hypothesis or report its broken invariant."""
    active_hypothesis = engine.state.active_hypothesis
    if active_hypothesis is None:
        message = "hypothesis engine did not retain the current hypothesis"
        raise RuntimeError(message)
    return active_hypothesis


def _begin_agent_round(
    ctx: LoopContext,
    records: list[RoundRecord],
    run_state: AgentRunState,
    progress_path: Path,
    progress: RoundProgress,
) -> None:
    """Select the round log, refresh its Pareto archive, and print its heading."""
    ctx.switch_log_file(f"round{progress.round_number:03d}")
    issue_board.write_pareto_archive(
        progress_path,
        _pareto_archive_summary(records, run_state.metrics),
    )
    ctx.lprint(f"\n{'=' * 60}\n  {progress.label()}\n{'=' * 60}\n")


def _finish_agent_rounds(
    ctx: LoopContext,
    records: list[RoundRecord],
    metrics: MetricSpace,
    progress_path: Path,
    max_rounds: int,
) -> bool:
    """Finalize the completed loop after it reaches its round budget."""
    ctx.lprint(f"Reached max_rounds={max_rounds}. Stopping.")
    _finalize_agent_run(ctx, records=records, space=metrics, progress_path=progress_path)
    return True


def _run_agent_rounds(
    ctx: LoopContext,
    request: LoopRunRequest,
    start_round: int | None,
) -> bool:
    """Restore durable loop state, run the requested rounds, and finalize."""
    max_rounds = request.max_rounds if request.max_rounds is not None else 24
    inner_loop = request.inner_loop
    session = _prepare_agent_round_session(ctx, request, start_round)
    agent_run_state = session.run_state
    round_history = session.history
    records = round_history.records
    carry = session.carry
    progress_path = session.progress_path
    round_number = session.round_number
    round_limit_result = _handle_agent_round_limit(
        ctx, request, round_history, progress_path, round_number
    )
    if round_limit_result is not None:
        return round_limit_result

    engine = _create_agent_hypothesis_engine(request, agent_run_state)
    try:
        while round_number <= max_rounds:
            round_progress = RoundProgress(round_number, max_rounds)
            _begin_agent_round(ctx, records, agent_run_state, progress_path, round_progress)
            with ctx.progress(round_progress):
                engine, plan = _plan_agent_round(ctx, request, engine, carry, round_progress)
                agent_run_state = engine.state
                active_hypothesis = _require_active_hypothesis(engine)
                engine = _apply_agent_rollback(ctx, request, plan, round_history, engine)
                agent_run_state = engine.state
                active_hypothesis = _require_active_hypothesis(engine)
                engine, attempt_outcome = _run_agent_attempts(
                    ctx, request, engine, round_history, round_progress
                )
                completed_record = _build_completed_round_record(
                    ctx, request, engine, round_history, attempt_outcome
                )
                agent_run_state = engine.state
                active_hypothesis = _require_active_hypothesis(engine)
                records = round_history.records
                round_number = attempt_outcome.round_number
                single_agent_response = attempt_outcome.single_agent_response
                if inner_loop == "single-agent" and single_agent_response is not None:
                    carry.single_agent_response = single_agent_response
                engine = _begin_completed_agent_round(
                    ctx, request, engine, completed_record, attempt_outcome
                )
                records.append(completed_record)

                _update_round_carry_over(
                    carry, active_hypothesis, round_history, attempt_outcome, progress_path
                )

                _persist_completed_agent_round(ctx, engine, completed_record, attempt_outcome)
                agent_run_state = engine.state
                active_hypothesis = agent_run_state.active_hypothesis

                round_number += 1

        return _finish_agent_rounds(
            ctx, records, agent_run_state.metrics, progress_path, max_rounds
        )
    finally:
        ctx.close()


def run_agent_loop(
    request: LoopRunRequest,
    *,
    integration: LocalRunIntegration | None = None,
    start_round: int | None = None,
) -> bool:
    """Run the orchestrator-driven build loop.

    Returns True iff the orchestrator declared the objective met within
    ``max_rounds``. Returns False when the round budget is exhausted.
    """
    existing = request.resume is not None
    start_round = (1 if not existing else None) if start_round is None else start_round
    ctx = _create_agent_run_context(request, integration)
    output_sink().run_configured(
        RunConfiguredData(
            run_log_path=str(ctx.run_log_path),
            project_root=str(ctx.project_root),
            objective=request.objective or request.input_bundle.objective,
        )
    )
    return _run_agent_rounds(ctx, request, start_round)
