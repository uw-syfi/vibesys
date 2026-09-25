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
from typing import TYPE_CHECKING, Any

from vibesys.agent_spec_config import resolve_agent_driver
from vibesys.context import create_run_context
from vibesys.domains.registry import resolve_domain
from vibesys.events import RunConfiguredData
from vibesys.loops.plain.render import render_all
from vibesys.loops.plain.runner_ext import PlainLoopAgentClient
from vibesys.loops.plain.state import PlainStateStore
from vibesys.prompts import PROMPTS_DIR, Prompt
from vibesys.render.sink import output_sink
from vibesys.run import LocalRunIntegration, LoopContext, RunStateNamespace
from vibesys.sandbox.run_environment import (
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
from vs_agent.api import AgentBackend, RoundProgress
from vs_issue_board.api import (
    Issue,
    IssueBoard,
    IssueStatus,
    IssueType,
)
from vs_loop_state.api import PlainLoopCursor, PlainPerformanceRecord
from vs_project.api import PlainRunConfiguration

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.loops.request import LoopRunRequest
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


def _run_implementer(
    ctx: LoopContext, prompt: Prompt, store: IssueBoard, issue: Issue, iteration: int
) -> Issue:
    """Run the implementer and persist its issue-board and progress updates."""
    ctx.reselect_gpu()
    system_prompt = prompt.render(
        "implementer/system.j2",
        reference_path=ctx.ref_name,
        runtime_notes=ctx.run_environment_view.prompt_notes,
        issue=issue,
    )
    user_prompt = prompt.render(
        "implementer/user.j2",
        issue=issue,
        prior_judge_review=_latest_judge_review(issue),
    )
    ctx.wait_for_debug(f"Implementer step on issue #{issue.id}")
    ctx.lprint(f">>> Implementer working on issue #{issue.id}...")
    issue_id = issue.id
    response = ctx.invoke(
        kind="implementer",
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_cls=IssueImplementerResponse,
        fallback_factory=lambda: IssueImplementerResponse(
            issue_id=issue_id,
            summary="Implementer did not produce a structured response.",
            files_touched=[],
            self_check="No structured response received.",
        ),
        round_label=f"impl issue #{issue.id} att{issue.attempts + 1}",
    )
    issue = store.increment_attempts(
        issue.id,
        actor="implementer",
        iteration=iteration,
        note=response.summary[:200],
        payload=response.model_dump(mode="json"),
    )
    local_dir = ctx.state.local(RunStateNamespace.PLAIN).external_directory()
    _update_progress_from_implementer(_init_progress(local_dir), iteration, issue, response)
    ctx.snapshot_workspace(f"iter-{iteration}-impl-{issue.id}-att{issue.attempts}")
    ctx.lprint(f"[snapshot] iter-{iteration}-impl-{issue.id}-att{issue.attempts}")
    return issue


def _run_judge(
    ctx: LoopContext, prompt: Prompt, store: IssueBoard, issue: Issue, iteration: int
) -> IssueJudgeResponse:
    """Run the judge and synchronize its issue-board and progress updates."""
    ctx.reselect_gpu()
    system_prompt = prompt.render(
        "judge/system.j2",
        accuracy_command=ctx.judge_accuracy_command,
        benchmark_command=ctx.judge_benchmark_command,
        issue=issue,
    )
    user_prompt = prompt.render("judge/user.j2", issue=issue)
    ctx.wait_for_debug(f"Judge step on issue #{issue.id}")
    ctx.lprint(f"\n>>> Judge reviewing issue #{issue.id}...")
    issue_id = issue.id
    response = ctx.invoke(
        kind="judge",
        iteration=iteration,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_cls=IssueJudgeResponse,
        fallback_factory=lambda: IssueJudgeResponse(
            issue_id=issue_id,
            analysis="No structured response received from judge.",
            feedback="Judge did not produce a structured response.",
            verdict=Verdict.FAIL,
            new_issues_filed=[],
        ),
        round_label=f"judge issue #{issue.id} att{issue.attempts}",
    )
    store.reload()
    local_dir = ctx.state.local(RunStateNamespace.PLAIN).external_directory()
    render_all(local_dir / "issues", store)
    progress_path = _init_progress(local_dir)
    _update_progress_from_judge(progress_path, iteration, issue, response)
    ctx.snapshot_workspace(f"iter-{iteration}-judge-{issue.id}-att{issue.attempts}")
    ctx.lprint(f">>> Judge verdict on #{issue.id}: {response.verdict.value.upper()}")
    return response


def _run_perf_evaluation(
    ctx: LoopContext,
    prompt: Prompt,
    store: IssueBoard,
    request: LoopRunRequest,
    iteration: int,
) -> IssuePerfEvalResponse:
    """Run the performance evaluator and persist its result artifacts."""
    ctx.reselect_gpu()
    portable_namespace = ctx.state.portable(RunStateNamespace.PLAIN)
    local_dir = ctx.state.local(RunStateNamespace.PLAIN).external_directory()
    perf_metrics_location = portable_namespace.agent_visible_path("perf/metrics.json")
    system_prompt = prompt.render(
        "perf_eval/system.j2",
        load_levels=request.config.perf_eval.load_levels,
        progress_path=None,
        perf_metrics_path=perf_metrics_location,
        issue_create_cap=request.max_issues_per_perf_eval,
        benchmark_command=ctx.judge_benchmark_command,
        runtime_notes=ctx.run_environment_view.prompt_notes,
    )
    user_prompt = prompt.render("perf_eval/user.j2")
    ctx.wait_for_debug("Perf evaluator step")
    ctx.lprint("\n>>> Performance Evaluator benchmarking...")
    response = ctx.invoke(
        kind="perf_eval",
        iteration=iteration,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        response_cls=IssuePerfEvalResponse,
        fallback_factory=lambda: IssuePerfEvalResponse(
            analysis="No structured response received from perf evaluator.",
            metrics=PerfMetrics(load_levels=[]),
            evaluator_feedback=[],
            new_issue_ids=[],
            throughput_trend=PerfTrend.MIXED,
            latency_trend=PerfTrend.MIXED,
        ),
        round_label=f"perf_eval iter {iteration}",
    )
    store.reload()
    render_all(local_dir / "issues", store)
    _update_progress_from_perf_eval(_init_progress(local_dir), iteration, response)
    PlainStateStore(portable_namespace).append_performance(
        PlainPerformanceRecord(
            iteration=iteration,
            timestamp=datetime.now(UTC),
            throughput_trend=response.throughput_trend.value,
            latency_trend=response.latency_trend.value,
            metrics=response.metrics.model_dump(mode="json"),
            new_issue_ids=tuple(response.new_issue_ids),
        )
    )
    ctx.snapshot_workspace(f"iter-{iteration}-perf_eval")
    ctx.lprint(
        f"\n>>> Perf trend: throughput={response.throughput_trend.value.upper()}, "
        f"latency={response.latency_trend.value.upper()}"
    )
    return response


def _initialize_plain_loop(
    ctx: LoopContext,
    request: LoopRunRequest,
    resume_state: PlainLoopState | None,
    prompt: Prompt,
) -> tuple[PlainStateStore, IssueBoard, PlainLoopState, int, str, int | None, int]:
    """Open issue state, bootstrap the queue, and resolve the resume cursor."""
    portable_namespace = ctx.state.portable(RunStateNamespace.PLAIN)
    state_store = PlainStateStore(portable_namespace)
    local_dir = ctx.state.local(RunStateNamespace.PLAIN).external_directory()
    issues_dir = local_dir / "issues"
    store_path = ctx.workspace / "issues.json"
    store: IssueBoard
    store = IssueBoard(store_path, on_change=lambda: render_all(issues_dir, store))
    render_all(issues_dir, store)
    ctx.agent_client = PlainLoopAgentClient(
        ctx.agent_client,
        max_issues_per_perf_eval=request.max_issues_per_perf_eval,
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
    if request.resume is not None or persisted_state is not None or resume_state is not None:
        reopened = store.reopen_blocked(
            actor="loop:resume",
            iteration=max(state.round_idx + 1, 1),
            note="retried on resume",
        )
        if reopened:
            ids = ", ".join(f"#{issue_id}" for issue_id in reopened)
            ctx.lprint(
                f"[resume] reopened {len(reopened)} previously blocked issue(s) for retry: {ids}"
            )
    i, next_phase, pending_issue_id = _determine_resume_point(state, store)
    end_iteration = i + (request.max_rounds if request.max_rounds is not None else 5)
    if request.resume is not None or persisted_state is not None or resume_state is not None:
        ctx.lprint(
            f"Resuming at round {i + 1} phase '{next_phase}'"
            + (f" issue #{pending_issue_id}" if pending_issue_id else "")
            + f", running up to {end_iteration - i} more rounds"
        )
    return state_store, store, state, i, next_phase, pending_issue_id, end_iteration


def _stop_if_all_remaining_issues_are_blocked(
    ctx: LoopContext,
    store: IssueBoard,
    state_store: PlainStateStore,
    state: PlainLoopState,
    iteration: int,
) -> bool:
    """Persist the cursor and stop when every unresolved issue is blocked."""
    remaining = [issue for issue in store.list() if issue.status is not IssueStatus.CLOSED]
    if not remaining or not all(issue.status is IssueStatus.BLOCKED for issue in remaining):
        return False
    ctx.lprint(f"[stop] all remaining issues are blocked ({len(remaining)} blocked); bailing out.")
    state = state.transition(
        round_idx=iteration,
        phase="perf_eval",
        current_issue_id=None,
    )
    _checkpoint_state(
        ctx,
        state_store,
        state,
        label="plain: record blocked issue queue",
    )
    return True


def _record_judge_result(
    store: IssueBoard,
    issue: Issue,
    response: IssueJudgeResponse,
    iteration: int,
) -> None:
    """Apply the judge verdict and persist its structured response."""
    if response.verdict is Verdict.PASS:
        status = IssueStatus.CLOSED
        note = f"closed by judge after attempt {issue.attempts}"
    else:
        status = IssueStatus.OPEN
        note = response.feedback[:500]
    store.update_status(
        issue.id,
        status,
        actor="judge",
        iteration=iteration,
        note=note,
        payload=response.model_dump(mode="json"),
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _drain_open_issues(
    ctx: LoopContext,
    request: LoopRunRequest,
    prompt: Prompt,
    store: IssueBoard,
    state: PlainLoopState,
) -> PlainLoopState:
    """Process the selected resume issue and all currently open work."""
    state_store = PlainStateStore(ctx.state.portable(RunStateNamespace.PLAIN))
    i, next_phase, pending_issue_id = _determine_resume_point(state, store)
    iter_label = i + 1
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

        if issue.attempts >= request.max_attempts_per_issue:
            store.update_status(
                issue.id,
                IssueStatus.BLOCKED,
                actor="loop",
                iteration=iter_label,
                note=f"exhausted {request.max_attempts_per_issue} attempts",
            )
            ctx.lprint(f"[block] issue #{issue.id} blocked after {issue.attempts} attempts")
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
            # Implementer has no issue-tracker tools — the relevant
            # issue is inlined into its system prompt — so no
            # .mcp.json sandwich here.
            issue = _run_implementer(ctx, prompt, store, issue, iter_label)

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
        # PlainLoopAgentClient injects tracker access (an
        # MCPServerSpec) for kind="judge" — see
        # vibesys/plain/runner_ext.py. The judge may file
        # at most ONE bug-type issue per review; that policy is
        # enforced by the wrapper.
        judge_response = _run_judge(ctx, prompt, store, issue, iter_label)

        _record_judge_result(store, issue, judge_response, iter_label)

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

    return state


def run_plain_loop(
    request: LoopRunRequest,
    integration: LocalRunIntegration | None = None,
    *,
    resume_state: PlainLoopState | None = None,
) -> bool:
    """Run the issue-tracker driven loop.

    Returns ``True`` if the loop terminates with no remaining open issues
    (everything resolved). Returns ``False`` if the iteration budget is
    exhausted with open work remaining, or if the run gets stuck (every
    remaining issue is BLOCKED).
    """
    bundle = request.input_bundle
    config = request.config
    exp_name = request.resume.run_id if request.resume is not None else request.exp_name
    if exp_name is None:
        message = "RunRequest.exp_name must be set for a fresh (non-resume) run"
        raise ValueError(message)
    run_environment = request.run_environment or make_run_environment_spec()
    resolved_agent_backend = str(request.agent_backend or config.agent.backend or AgentBackend.CLI)
    max_rounds = request.max_rounds if request.max_rounds is not None else 5
    run_configuration = PlainRunConfiguration(
        outer_loop="plain",
        run_environment=run_environment_record(run_environment),
        model=config.model.name,
        agent_backend=resolved_agent_backend,
        agent_driver=(
            resolve_agent_driver(config).value if resolved_agent_backend == "cli" else None
        ),
        cli_provider=request.cli_provider or config.agent.cli_provider or "codex",
        cli_timeout=config.agent.cli_timeout,
        compute_backend=request.backend.value,
        profiler=request.profiler_kind.value,
        modality=None,
        default_reasoning_effort=config.thinking.level,
        outer_model=config.agent.outer.model,
        outer_reasoning_effort=config.agent.outer.reasoning_effort,
        inner_model=config.agent.inner.model,
        inner_reasoning_effort=config.agent.inner.reasoning_effort,
        max_rounds=max_rounds,
        max_attempts_per_issue=request.max_attempts_per_issue,
        max_issues_per_perf_eval=request.max_issues_per_perf_eval,
    )

    with create_run_context(
        config=config,
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
        existing=request.resume is not None,
        debug=request.debug,
        profiler_kind=request.profiler_kind,
        skills_dirs=request.skills_dirs,
        run_environment=run_environment,
        project_configuration=run_configuration,
        agent_backend=request.agent_backend,
        cli_provider=request.cli_provider,
        backend=request.backend,
        environment_hooks=resolve_domain(bundle.domain).environment_hooks,
        remote_repo=request.remote_repo,
        repo_visibility=request.repo_visibility,
        integration=integration,
    ) as ctx:
        output_sink().run_configured(
            RunConfiguredData(
                run_log_path=str(ctx.run_log_path),
                project_root=str(ctx.project_root),
                model=ctx.model_name,
            )
        )
        prompt = Prompt(_TEMPLATE_DIR, ctx.backend)
        state_store, store, state, i, _next_phase, _pending_issue_id, end_iteration = (
            _initialize_plain_loop(ctx, request, resume_state, prompt)
        )

        while i < end_iteration:
            iter_label = i + 1
            round_progress = RoundProgress(iter_label, end_iteration)
            ctx.lprint(f"\n{'=' * 60}")
            ctx.lprint(f"  {round_progress.label()}")
            ctx.lprint(f"{'=' * 60}\n")

            with ctx.progress(round_progress):
                state = _drain_open_issues(ctx, request, prompt, store, state)
                i = state.round_idx
                # ---------------------------------------------------------------
                # PERF_EVAL phase (after drain complete)
                # ---------------------------------------------------------------
                # Bail-out check: if every remaining issue is BLOCKED, we're stuck.
                if _stop_if_all_remaining_issues_are_blocked(
                    ctx,
                    store,
                    state_store,
                    state,
                    i,
                ):
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
                # PlainLoopAgentClient injects tracker access for kind="perf_eval"
                # and scopes the per-iteration cap by the iteration kwarg below,
                # so issues filed here are counted against iter_label's budget.
                # See vibesys/plain/runner_ext.py.
                perf_response = _run_perf_evaluation(ctx, prompt, store, request, iter_label)
                _checkpoint_state(
                    ctx,
                    state_store,
                    state,
                    label=f"plain: record performance evaluation {iter_label}",
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
