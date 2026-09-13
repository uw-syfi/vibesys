"""Issue-tracker driven loop.

Outer flow per iteration:
  1. Drain all OPEN issues: pick next → fresh implementer → fresh judge → close on PASS
     or leave open with feedback on FAIL. Issues exhausting their attempt budget are
     marked BLOCKED and skipped.
  2. Once the queue is drained, run the perf evaluator. The perf evaluator may file
     up to ``max_issues_per_perf_eval`` new issues via the create_issue tool, capped
     server-side.
  3. Loop back to step 1 with the new issues.

The very first iteration auto-creates one bootstrap FEATURE issue describing the
LLM serving build task (rendered from ``prompts/loops/plain/bootstrap_issue.j2``), so the
implementer phase always has something to chew on.

State machine: ``PlainLoopState`` (in ``state.json``) tracks only the cursor —
which iteration we're in, which issue is currently being processed, and what
phase we're in. The store (in ``issues.json``) is the source of truth for which
issues exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import Any

from vibesys.agents.factory import resolve_agent_driver
from vibesys.agents.progress import RoundProgress
from vibesys.config import Config, as_config
from vibesys.constants import DEFAULT_AGENT_BACKEND, DEFAULT_COMPUTE_BACKEND, ComputeBackend
from vibesys.context import create_run_context
from vibesys.domains.base import DomainName  # noqa: TC001  # tracked: #288
from vibesys.domains.registry import resolve_domain
from vibesys.input_manifest import WorkspaceSource  # noqa: TC001  # tracked: #288
from vibesys.loops.plain.render import render_all
from vibesys.loops.plain.runner_ext import PlainLoopAgentClient
from vibesys.loops.plain.state import PlainStateStore
from vibesys.profilers import ProfilerKind
from vibesys.prompts import PROMPTS_DIR, Prompt
from vibesys.render.sink import output_sink
from vibesys.run import LoopContext, RepositoryVisibility, RunIntegration, RunStateNamespace
from vibesys.sandbox.run_environment import (
    RunEnvironmentSpec,
    make_run_environment_spec,
    run_environment_record,
)
from vibesys.schemas import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    PerfMetrics,
    PerfTrend,
    Verdict,
)
from vs_issue_board import (
    Issue,
    IssueBoard,
    IssueStatus,
    IssueType,
)
from vs_loop_state import PlainLoopCursor, PlainPerformanceRecord
from vs_project import PlainRunConfiguration

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "plain"
PlainLoopState = PlainLoopCursor


def _checkpoint_state(
    ctx: LoopContext,
    store: PlainStateStore,
    state: PlainLoopState,
    *,
    label: str,
) -> None:
    """Write and commit a recoverable plain-loop checkpoint."""
    store.save_cursor(state)
    ctx.state.commit(label, store.namespace)


def _determine_resume_point(
    state: PlainLoopState | None, store: IssueBoard
) -> tuple[int, str, int | None]:
    """Return ``(iteration, phase, current_issue_id)`` to resume from.

    *iteration* is 0-indexed.
    """
    if state is None:
        return 0, "implementer", None

    # Mid-judge crash: re-run the judge for the same issue
    if state.phase == "judge" and state.current_issue_id is not None:
        issue = store.get(state.current_issue_id)
        if issue is not None and issue.status in (IssueStatus.IN_PROGRESS, IssueStatus.OPEN):
            return state.round_idx, "judge", state.current_issue_id

    # Mid-implementer crash: re-run the implementer for the same issue
    if state.phase == "implementer" and state.current_issue_id is not None:
        issue = store.get(state.current_issue_id)
        if issue is not None and issue.status in (IssueStatus.IN_PROGRESS, IssueStatus.OPEN):
            return state.round_idx, "implementer", state.current_issue_id

    # Otherwise: drain remaining open issues, then fall through to perf_eval.
    # _determine_resume_point never returns "perf_eval" — the drain loop in
    # run_plain_loop short-circuits to perf_eval naturally when next_open()
    # returns None.
    return state.round_idx, "implementer", None


# ---------------------------------------------------------------------------
# Progress markdown helpers
# ---------------------------------------------------------------------------


def _init_progress(log_dir: Path) -> Path:
    progress_path = log_dir / "progress.md"
    if not progress_path.exists():
        progress_path.write_text("# Experiment Progress\n\n")
    return progress_path


def _update_progress_from_implementer(
    progress_path: Path,
    iteration: int,
    issue: Issue,
    response: IssueImplementerResponse,
) -> None:
    with progress_path.open("a", encoding="utf-8") as f:
        f.write(f"## Iter {iteration} — Implementer on issue #{issue.id}\n\n")
        f.write(f"**Issue**: [{issue.type.value}] {issue.title}\n\n")
        f.write(f"**Summary**: {response.summary}\n\n")
        if response.files_touched:
            f.write("**Files touched**:\n")
            for fp in response.files_touched:
                f.write(f"- `{fp}`\n")
            f.write("\n")
        f.write(f"**Self-check**: {response.self_check}\n\n")


def _update_progress_from_judge(
    progress_path: Path,
    iteration: int,
    issue: Issue,
    response: IssueJudgeResponse,
) -> None:
    with progress_path.open("a", encoding="utf-8") as f:
        f.write(f"### Iter {iteration} — Judge on issue #{issue.id}\n\n")
        f.write(f"**Verdict**: {response.verdict.value.upper()}\n\n")
        f.write(f"**Analysis**: {response.analysis}\n\n")
        if response.feedback:
            f.write(f"**Feedback**: {response.feedback}\n\n")
        if response.new_issues_filed:
            ids = ", ".join(f"#{i}" for i in response.new_issues_filed)
            f.write(f"**New issues filed**: {ids}\n\n")


def _update_progress_from_perf_eval(
    progress_path: Path,
    iteration: int,
    response: IssuePerfEvalResponse,
) -> None:
    with progress_path.open("a", encoding="utf-8") as f:
        f.write(f"## Iter {iteration} — Performance Evaluator\n\n")
        f.write(f"**Throughput trend**: {response.throughput_trend.value.upper()}\n\n")
        f.write(f"**Latency trend**: {response.latency_trend.value.upper()}\n\n")
        f.write(f"**Analysis**: {response.analysis}\n\n")
        if response.new_issue_ids:
            ids = ", ".join(f"#{i}" for i in response.new_issue_ids)
            f.write(f"**New issues filed**: {ids}\n\n")
        if response.evaluator_feedback:
            f.write("**Notes for next perf evaluator**:\n")
            for note in response.evaluator_feedback:
                f.write(f"- {note}\n")
            f.write("\n")


# ---------------------------------------------------------------------------
# Agent-visible memory
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Implementer retry context
# ---------------------------------------------------------------------------


def _latest_judge_review(issue: Issue) -> dict[str, Any] | None:
    """Return the most recent judge FAIL review on this issue, or ``None``.

    Walks ``issue.history`` in reverse looking for a status-transition
    event whose ``actor`` is ``"judge"``. Such events only happen on a
    judge verdict (PASS → CLOSED, FAIL → OPEN); since the implementer is
    only invoked while the issue is OPEN/IN_PROGRESS, any judge event in
    history must have been a FAIL — the corresponding feedback is what we
    want to surface to the next implementer attempt.

    Prefers the structured ``payload`` (added in the per-issue MD feature)
    but falls back to the truncated ``note`` for backwards compatibility
    with pre-payload runs. Returns ``None`` if there's no prior judge
    review or both feedback/analysis are empty.
    """
    for evt in reversed(issue.history):
        if evt.actor != "judge" or "->" not in evt.action:
            continue
        payload = evt.payload or {}
        feedback = (payload.get("feedback") or evt.note or "").strip()
        analysis = (payload.get("analysis") or "").strip()
        if not feedback and not analysis:
            return None
        return {
            "feedback": feedback,
            "analysis": analysis,
            "iteration": evt.iteration,
        }
    return None


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _ensure_bootstrap_issue(
    store: IssueBoard,
    *,
    state: PlainLoopState,
    state_store: PlainStateStore,
    ctx: LoopContext,
    prompt: Prompt,
) -> None:
    """Auto-create the initial feature issue on the first run.

    Idempotent on resume — checks state.bootstrap_done first.
    """
    if state.bootstrap_done:
        return
    description = prompt.render(
        "bootstrap_issue.j2",
        reference_path=ctx.ref_name,
        accuracy_command=ctx.judge_accuracy_command,
        benchmark_command=ctx.judge_benchmark_command,
        runtime_notes=ctx.run_environment_view.prompt_notes,
    )
    issue = store.create(
        type=IssueType.FEATURE,
        title="Build FastAPI inference server for the reference model",
        description=description,
        created_by="loop:bootstrap",
        iteration=max(state.round_idx + 1, 1),
    )
    state.bootstrap_done = True
    _checkpoint_state(
        ctx,
        state_store,
        state,
        label="plain: initialize issue board",
    )
    ctx.lprint(f"[bootstrap] created initial issue #{issue.id}")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_plain_loop(  # noqa: C901, PLR0912, PLR0913, PLR0915  # tracked: #288
    config: Config,
    exp_name: str,
    input_path: str,
    accuracy_command: str,
    benchmark_command: str,
    *,
    runs_dir: Path | None,
    task_name: str | None = None,
    task_root: Path | None = None,
    workspace_sources: tuple[WorkspaceSource, ...] = (),
    evaluator_path: Path | None = None,
    evaluator_package_root: Path | None = None,
    max_rounds: int = 5,
    max_attempts_per_issue: int = 3,
    max_issues_per_perf_eval: int = 3,
    existing: bool = False,
    resume_state: PlainLoopState | None = None,
    debug: bool = False,
    profiler_kind: ProfilerKind = ProfilerKind.AUTO,
    skills_dirs: list[str] | None = None,
    run_environment: RunEnvironmentSpec | None = None,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
    domain: DomainName,
    remote_repo: str | None = None,
    repo_visibility: RepositoryVisibility = RepositoryVisibility.PRIVATE,
    integration: RunIntegration | None = None,
) -> bool:
    """Run the issue-tracker driven loop.

    Returns ``True`` if the loop terminates with no remaining open issues
    (everything resolved). Returns ``False`` if the iteration budget is
    exhausted with open work remaining, or if the run gets stuck (every
    remaining issue is BLOCKED).
    """
    config = as_config(config)
    domain_definition = resolve_domain(domain)
    run_environment = run_environment or make_run_environment_spec()
    resolved_agent_backend = agent_backend or config.agent.backend or DEFAULT_AGENT_BACKEND
    run_configuration = PlainRunConfiguration(
        outer_loop="plain",
        run_environment=run_environment_record(run_environment),
        model=config.model.name,
        agent_backend=resolved_agent_backend,
        agent_driver=(resolve_agent_driver(config) if resolved_agent_backend == "cli" else None),
        cli_provider=cli_provider or config.agent.cli_provider or "codex",
        cli_timeout=config.agent.cli_timeout,
        compute_backend=backend.value,
        profiler=profiler_kind.value,
        modality=None,
        default_reasoning_effort=config.thinking.level,
        outer_model=config.agent.outer.model,
        outer_reasoning_effort=config.agent.outer.reasoning_effort,
        inner_model=config.agent.inner.model,
        inner_reasoning_effort=config.agent.inner.reasoning_effort,
        max_rounds=max_rounds,
        max_attempts_per_issue=max_attempts_per_issue,
        max_issues_per_perf_eval=max_issues_per_perf_eval,
    )

    with create_run_context(
        config=config,
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
        existing=existing,
        debug=debug,
        profiler_kind=profiler_kind,
        skills_dirs=skills_dirs,
        run_environment=run_environment,
        project_configuration=run_configuration,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backend=backend,
        environment_hooks=domain_definition.environment_hooks,
        remote_repo=remote_repo,
        repo_visibility=repo_visibility,
        integration=integration,
    ) as ctx:
        output_sink().run_configured(
            run_log_path=str(ctx.run_log_path),
            project_root=str(ctx.project_root),
            model=ctx.model_name,
        )
        prompt = Prompt(_TEMPLATE_DIR, ctx.backend)
        portable_namespace = ctx.state.portable(RunStateNamespace.PLAIN)
        state_store = PlainStateStore(portable_namespace)
        local_dir = ctx.state.local(RunStateNamespace.PLAIN).external_directory()

        progress_path = _init_progress(local_dir)
        perf_metrics_location = portable_namespace.agent_visible_path("perf/metrics.json")
        issues_dir = local_dir / "issues"

        # The issue board is deliberately agent-visible project memory. CLI
        # tracker tools run inside the candidate sandbox, where framework state
        # is read-only. The framework cursor and performance history remain in
        # the run's portable state namespace.
        store_path = ctx.workspace / "issues.json"

        # Wire the per-issue markdown renderer as a store on_change hook
        # so every successful save (including tool-created issues from
        # judge/perf_eval) re-renders the human-readable mirror.
        # Forward-declare `store` so the lambda's late binding resolves.
        store: IssueBoard
        store = IssueBoard(
            store_path,
            on_change=lambda: render_all(issues_dir, store),
        )
        # Render immediately so local diagnostics are complete even if the
        # resumed run performs no issue-board mutation.
        render_all(issues_dir, store)

        # Wrap the runner so judge/perf_eval invokes auto-receive issue
        # tracker access (in-process @tool callables under deepagents,
        # MCP server spec under cli). The wrapper consumes an extra
        # ``iteration=`` kwarg on invoke() that the loop passes per call.
        # See vibesys/plain/runner_ext.py.
        # The wrapper preserves the AgentClient surface while adding issue tools.
        ctx.agent_client = PlainLoopAgentClient(
            ctx.agent_client,
            store=store,
            max_issues_per_perf_eval=max_issues_per_perf_eval,
        )

        persisted_state = state_store.load_cursor()
        state = persisted_state or resume_state or PlainLoopState()
        _ensure_bootstrap_issue(
            store,
            state=state,
            state_store=state_store,
            ctx=ctx,
            prompt=prompt,
        )

        # On resume, give every previously BLOCKED issue a fresh attempt
        # budget. The common reason a user resumes is that the prior run
        # bailed out because every remaining issue was blocked; without
        # this reset the resumed run would simply bail out again on the
        # first drain pass.
        if existing or persisted_state is not None or resume_state is not None:
            reopened = store.reopen_blocked(
                actor="loop:resume",
                iteration=max(state.round_idx + 1, 1),
                note="retried on resume",
            )
            if reopened:
                ids = ", ".join(f"#{i}" for i in reopened)
                ctx.lprint(
                    f"[resume] reopened {len(reopened)} previously blocked "
                    f"issue(s) for retry: {ids}"
                )

        # Determine where to resume from
        i, next_phase, pending_issue_id = _determine_resume_point(state, store)
        end_iteration = i + max_rounds

        if existing or persisted_state is not None or resume_state is not None:
            ctx.lprint(
                f"Resuming at round {i + 1} phase '{next_phase}'"
                + (f" issue #{pending_issue_id}" if pending_issue_id else "")
                + f", running up to {max_rounds} more rounds"
            )

        load_levels = config.perf_eval.load_levels
        while i < end_iteration:
            iter_label = i + 1
            round_progress = RoundProgress(iter_label, end_iteration)
            ctx.lprint(f"\n{'=' * 60}")
            ctx.lprint(f"  {round_progress.label()}")
            ctx.lprint(f"{'=' * 60}\n")

            with ctx.progress(round_progress):
                # ---------------------------------------------------------------
                # DRAIN open issues
                # ---------------------------------------------------------------
                while True:
                    # If we're resuming with a specific issue, pick that one first.
                    if pending_issue_id is not None:
                        issue = store.get(pending_issue_id)
                        pending_issue_id = None
                    else:
                        issue = store.next_open()

                    if issue is None:
                        break

                    if issue.attempts >= max_attempts_per_issue:
                        store.update_status(
                            issue.id,
                            IssueStatus.BLOCKED,
                            actor="loop",
                            iteration=iter_label,
                            note=f"exhausted {max_attempts_per_issue} attempts",
                        )
                        ctx.lprint(
                            f"[block] issue #{issue.id} blocked after {issue.attempts} attempts"
                        )
                        continue

                    # Claim the issue
                    if issue.status == IssueStatus.OPEN:
                        issue = store.update_status(
                            issue.id,
                            IssueStatus.IN_PROGRESS,
                            actor="loop",
                            iteration=iter_label,
                            note="claimed for processing",
                        )

                    # ----- Implementer phase -----
                    if next_phase != "judge":
                        state = state.transition(
                            round_idx=i,
                            phase="implementer",
                            current_issue_id=issue.id,
                        )
                        _checkpoint_state(
                            ctx,
                            state_store,
                            state,
                            label=f"plain: begin implementer for issue {issue.id}",
                        )
                        ctx.reselect_gpu()

                        impl_system_prompt = prompt.render(
                            "implementer/system.j2",
                            reference_path=ctx.ref_name,
                            runtime_notes=ctx.run_environment_view.prompt_notes,
                            issue=issue,
                        )
                        impl_prompt = prompt.render(
                            "implementer/user.j2",
                            issue=issue,
                            prior_judge_review=_latest_judge_review(issue),
                        )

                        ctx.wait_for_debug(f"Implementer step on issue #{issue.id}")
                        ctx.lprint(f">>> Implementer working on issue #{issue.id}...")
                        # Implementer has no issue-tracker tools — the relevant
                        # issue is inlined into its system prompt — so no
                        # .mcp.json sandwich here.
                        issue_id_for_fallback = issue.id
                        impl_response = ctx.invoke(
                            kind="implementer",
                            system_prompt=impl_system_prompt,
                            user_prompt=impl_prompt,
                            response_cls=IssueImplementerResponse,
                            fallback_factory=lambda issue_id=issue_id_for_fallback: (
                                IssueImplementerResponse(
                                    issue_id=issue_id,
                                    summary="Implementer did not produce a structured response.",
                                    files_touched=[],
                                    self_check="No structured response received.",
                                )
                            ),
                            round_label=f"impl issue #{issue.id} att{issue.attempts + 1}",
                        )

                        issue = store.increment_attempts(
                            issue.id,
                            actor="implementer",
                            iteration=iter_label,
                            note=impl_response.summary[:200],
                            payload=impl_response.model_dump(mode="json"),
                        )
                        _update_progress_from_implementer(
                            progress_path, iter_label, issue, impl_response
                        )
                        ctx.snapshot_workspace(
                            f"iter-{iter_label}-impl-{issue.id}-att{issue.attempts}"
                        )
                        ctx.lprint(
                            f"[snapshot] iter-{iter_label}-impl-{issue.id}-att{issue.attempts}"
                        )

                    # next_phase only kicks in for the first issue we resume on
                    next_phase = ""

                    # ----- Judge phase -----
                    state = state.transition(
                        round_idx=i,
                        phase="judge",
                        current_issue_id=issue.id,
                    )
                    _checkpoint_state(
                        ctx,
                        state_store,
                        state,
                        label=f"plain: begin judge for issue {issue.id}",
                    )
                    ctx.reselect_gpu()

                    judge_system_prompt = prompt.render(
                        "judge/system.j2",
                        accuracy_command=ctx.judge_accuracy_command,
                        benchmark_command=ctx.judge_benchmark_command,
                        issue=issue,
                    )
                    judge_prompt = prompt.render("judge/user.j2", issue=issue)

                    ctx.wait_for_debug(f"Judge step on issue #{issue.id}")
                    ctx.lprint(f"\n>>> Judge reviewing issue #{issue.id}...")
                    # PlainLoopAgentClient injects tracker access (in-process
                    # @tool callables under deepagents, MCPServerSpec under
                    # cli) for kind="judge" — see
                    # vibesys/plain/runner_ext.py. The judge may file
                    # at most ONE bug-type issue per review; that policy is
                    # enforced by the wrapper.
                    judge_issue_id = issue.id
                    judge_response = ctx.invoke(
                        kind="judge",
                        iteration=iter_label,
                        system_prompt=judge_system_prompt,
                        user_prompt=judge_prompt,
                        response_cls=IssueJudgeResponse,
                        fallback_factory=lambda issue_id=judge_issue_id: IssueJudgeResponse(
                            issue_id=issue_id,
                            analysis="No structured response received from judge.",
                            feedback="Judge did not produce a structured response.",
                            verdict=Verdict.FAIL,
                            new_issues_filed=[],
                        ),
                        round_label=f"judge issue #{issue.id} att{issue.attempts}",
                    )

                    # Under cli the MCP server writes via a separate IssueBoard
                    # on the same file, so reload picks up tool-created issues.
                    # Under deepagents the @tool callables mutate the in-memory
                    # store directly, so reload is a no-op there. reload() does
                    # not fire on_change, so re-render explicitly to keep the
                    # per-issue markdown view in sync.
                    store.reload()
                    render_all(issues_dir, store)

                    _update_progress_from_judge(progress_path, iter_label, issue, judge_response)
                    ctx.snapshot_workspace(
                        f"iter-{iter_label}-judge-{issue.id}-att{issue.attempts}"
                    )
                    ctx.lprint(
                        f">>> Judge verdict on #{issue.id}: {judge_response.verdict.value.upper()}"
                    )

                    if judge_response.verdict == Verdict.PASS:
                        store.update_status(
                            issue.id,
                            IssueStatus.CLOSED,
                            actor="judge",
                            iteration=iter_label,
                            note=f"closed by judge after attempt {issue.attempts}",
                            payload=judge_response.model_dump(mode="json"),
                        )
                    else:
                        store.update_status(
                            issue.id,
                            IssueStatus.OPEN,
                            actor="judge",
                            iteration=iter_label,
                            note=judge_response.feedback[:500],
                            payload=judge_response.model_dump(mode="json"),
                        )

                    state = state.transition(
                        round_idx=i,
                        phase="implementer",
                        current_issue_id=None,
                    )
                    _checkpoint_state(
                        ctx,
                        state_store,
                        state,
                        label=f"plain: record judge result for issue {issue.id}",
                    )
                    # Loop back to drain the next open issue.

                # ---------------------------------------------------------------
                # PERF_EVAL phase (after drain complete)
                # ---------------------------------------------------------------
                # Bail-out check: if every remaining issue is BLOCKED, we're stuck.
                remaining = [iss for iss in store.list() if iss.status not in (IssueStatus.CLOSED,)]  # noqa: FURB171  # tracked: #288
                blocked_only = remaining and all(
                    iss.status == IssueStatus.BLOCKED for iss in remaining
                )
                if blocked_only:
                    ctx.lprint(
                        f"[stop] all remaining issues are blocked "
                        f"({len(remaining)} blocked); bailing out."
                    )
                    state = state.transition(
                        round_idx=i,
                        phase="perf_eval",
                        current_issue_id=None,
                    )
                    _checkpoint_state(
                        ctx,
                        state_store,
                        state,
                        label="plain: record blocked issue queue",
                    )
                    return False

                state = state.transition(
                    round_idx=i,
                    phase="perf_eval",
                    current_issue_id=None,
                )
                _checkpoint_state(
                    ctx,
                    state_store,
                    state,
                    label=f"plain: begin performance evaluation {iter_label}",
                )
                ctx.reselect_gpu()

                perf_system_prompt = prompt.render(
                    "perf_eval/system.j2",
                    load_levels=load_levels,
                    progress_path=None,
                    perf_metrics_path=perf_metrics_location,
                    issue_create_cap=max_issues_per_perf_eval,
                    benchmark_command=ctx.judge_benchmark_command,
                    runtime_notes=ctx.run_environment_view.prompt_notes,
                )
                perf_prompt = prompt.render("perf_eval/user.j2")

                ctx.wait_for_debug("Perf evaluator step")
                ctx.lprint("\n>>> Performance Evaluator benchmarking...")
                # PlainLoopAgentClient injects tracker access for kind="perf_eval"
                # and scopes the per-iteration cap by the iteration kwarg below,
                # so issues filed here are counted against iter_label's budget.
                # See vibesys/plain/runner_ext.py.
                perf_response = ctx.invoke(
                    kind="perf_eval",
                    iteration=iter_label,
                    system_prompt=perf_system_prompt,
                    user_prompt=perf_prompt,
                    response_cls=IssuePerfEvalResponse,
                    fallback_factory=lambda: IssuePerfEvalResponse(
                        analysis="No structured response received from perf evaluator.",
                        metrics=PerfMetrics(load_levels=[]),
                        evaluator_feedback=[],
                        new_issue_ids=[],
                        throughput_trend=PerfTrend.MIXED,
                        latency_trend=PerfTrend.MIXED,
                    ),
                    round_label=f"perf_eval iter {iter_label}",
                )

                store.reload()
                render_all(issues_dir, store)

                _update_progress_from_perf_eval(progress_path, iter_label, perf_response)
                state_store.append_performance(
                    PlainPerformanceRecord(
                        iteration=iter_label,
                        timestamp=datetime.now(UTC),
                        throughput_trend=perf_response.throughput_trend.value,
                        latency_trend=perf_response.latency_trend.value,
                        metrics=perf_response.metrics.model_dump(mode="json"),
                        new_issue_ids=tuple(perf_response.new_issue_ids),
                    )
                )
                ctx.snapshot_workspace(f"iter-{iter_label}-perf_eval")
                _checkpoint_state(
                    ctx,
                    state_store,
                    state,
                    label=f"plain: record performance evaluation {iter_label}",
                )
                ctx.lprint(
                    f"\n>>> Perf trend: throughput={perf_response.throughput_trend.value.upper()}, "
                    f"latency={perf_response.latency_trend.value.upper()}"
                )

                # Termination check: nothing open AND perf_eval filed nothing → done.
                still_open = store.list(status=IssueStatus.OPEN)
                if not still_open and not perf_response.new_issue_ids:
                    ctx.lprint("[done] no open issues and perf_eval filed none.")
                    state = state.transition(
                        round_idx=i + 1,
                        phase="implementer",
                        current_issue_id=None,
                    )
                    _checkpoint_state(
                        ctx,
                        state_store,
                        state,
                        label=f"plain: complete performance evaluation {iter_label}",
                    )
                    return True

                i += 1
                state = state.transition(
                    round_idx=i,
                    phase="implementer",
                    current_issue_id=None,
                )
                _checkpoint_state(
                    ctx,
                    state_store,
                    state,
                    label=f"plain: complete round {iter_label}",
                )

        ctx.lprint("Run completed — round budget exhausted.")
        return False
