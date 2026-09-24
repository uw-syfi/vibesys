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

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING, Any

from vibesys.agent_spec_config import resolve_agent_driver
from vibesys.config import Config, LoadLevelCfg, as_config
from vibesys.constants import (
    DEFAULT_COMPUTE_BACKEND,
    ComputeBackend,
    DomainName,
)
from vibesys.context import create_run_context
from vibesys.domains.registry import resolve_domain
from vibesys.evaluators.input_manifest import WorkspaceSource  # noqa: TC001  # tracked: #288
from vibesys.loops.plain.orchestration import (
    PlainOrchestrationOptions,
    compare_resume,
    descriptor_from_options,
    legacy_configuration_from_options,
)
from vibesys.loops.plain.policy import PlainPolicy, _resume_point
from vibesys.loops.plain.render import render_all
from vibesys.loops.plain.runner_ext import PlainLoopAgentClient
from vibesys.loops.plain.state import PlainStateStore
from vibesys.profilers import ProfilerKind
from vibesys.prompts import PROMPTS_DIR, Prompt
from vibesys.render.sink import output_sink
from vibesys.run import LocalRunIntegration, LoopContext, RepositoryVisibility, RunStateNamespace
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
from vs_agent.api import AgentBackend, RoundProgress
from vs_issue_board.api import (
    Issue,
    IssueBoard,
    IssueStatus,
    IssueType,
)
from vs_loop_state.api import PlainLoopCursor, PlainPerformanceRecord

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "plain"
PlainLoopState = PlainLoopCursor

if TYPE_CHECKING:
    from collections.abc import Iterator


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
    """Compatibility helper for older callers of the plain-loop resume selector."""
    if state is None:
        return 0, "implementer", None
    return _resume_point(state, store.get)


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


@dataclass
class _PlainEffects:
    """Concrete turns, persistence, and workspace effects for PlainPolicy."""

    ctx: LoopContext
    board: IssueBoard
    state_store: PlainStateStore
    prompt: Prompt
    progress_path: Path
    issues_dir: Path
    perf_metrics_location: str
    max_issues_per_perf_eval: int
    load_levels: list[LoadLevelCfg] | None

    def bootstrap(self, state: PlainLoopState) -> None:
        _ensure_bootstrap_issue(
            self.board, state=state, state_store=self.state_store, ctx=self.ctx, prompt=self.prompt
        )

    def checkpoint(self, state: PlainLoopState, label: str) -> None:
        _checkpoint_state(self.ctx, self.state_store, state, label=label)

    @contextmanager
    def progress(self, iteration: int, total: int) -> Iterator[None]:
        progress = RoundProgress(iteration, total)
        self.ctx.lprint(f"\n{'=' * 60}")
        self.ctx.lprint(f"  {progress.label()}")
        self.ctx.lprint(f"{'=' * 60}\n")
        with self.ctx.progress(progress):
            yield

    def log(self, message: str) -> None:
        self.ctx.lprint(message)

    def get_issue(self, issue_id: int) -> Issue | None:
        return self.board.get(issue_id)

    def next_open_issue(self) -> Issue | None:
        return self.board.next_open()

    def list_issues(self, status: IssueStatus | None = None) -> list[Issue]:
        return self.board.list(status=status)

    def reopen_blocked(self, iteration: int) -> list[int]:
        return self.board.reopen_blocked(
            actor="loop:resume", iteration=iteration, note="retried on resume"
        )

    def claim(self, issue: Issue, iteration: int) -> Issue:
        return self.board.update_status(
            issue.id,
            IssueStatus.IN_PROGRESS,
            actor="loop",
            iteration=iteration,
            note="claimed for processing",
        )

    def block(self, issue: Issue, iteration: int, max_attempts: int) -> None:
        self.board.update_status(
            issue.id,
            IssueStatus.BLOCKED,
            actor="loop",
            iteration=iteration,
            note=f"exhausted {max_attempts} attempts",
        )

    def increment_attempts(
        self, issue: Issue, response: IssueImplementerResponse, iteration: int
    ) -> Issue:
        return self.board.increment_attempts(
            issue.id,
            actor="implementer",
            iteration=iteration,
            note=response.summary[:200],
            payload=response.model_dump(mode="json"),
        )

    def close_issue(self, issue: Issue, response: IssueJudgeResponse, iteration: int) -> None:
        self.board.update_status(
            issue.id,
            IssueStatus.CLOSED,
            actor="judge",
            iteration=iteration,
            note=f"closed by judge after attempt {issue.attempts}",
            payload=response.model_dump(mode="json"),
        )

    def reopen_issue(self, issue: Issue, response: IssueJudgeResponse, iteration: int) -> None:
        self.board.update_status(
            issue.id,
            IssueStatus.OPEN,
            actor="judge",
            iteration=iteration,
            note=response.feedback[:500],
            payload=response.model_dump(mode="json"),
        )

    def implement(self, issue: Issue) -> IssueImplementerResponse:
        ctx = self.ctx
        ctx.reselect_gpu()
        system_prompt = self.prompt.render(
            "implementer/system.j2",
            reference_path=ctx.ref_name,
            runtime_notes=ctx.run_environment_view.prompt_notes,
            issue=issue,
        )
        user_prompt = self.prompt.render(
            "implementer/user.j2",
            issue=issue,
            prior_judge_review=_latest_judge_review(issue),
        )
        ctx.wait_for_debug(f"Implementer step on issue #{issue.id}")
        ctx.lprint(f">>> Implementer working on issue #{issue.id}...")
        issue_id = issue.id
        return ctx.invoke(
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

    def record_implementation(
        self, issue: Issue, response: IssueImplementerResponse, iteration: int
    ) -> None:
        _update_progress_from_implementer(self.progress_path, iteration, issue, response)
        self.ctx.snapshot_workspace(f"iter-{iteration}-impl-{issue.id}-att{issue.attempts}")
        self.ctx.lprint(f"[snapshot] iter-{iteration}-impl-{issue.id}-att{issue.attempts}")

    def judge(self, issue: Issue, iteration: int) -> IssueJudgeResponse:
        ctx = self.ctx
        ctx.reselect_gpu()
        system_prompt = self.prompt.render(
            "judge/system.j2",
            accuracy_command=ctx.judge_accuracy_command,
            benchmark_command=ctx.judge_benchmark_command,
            issue=issue,
        )
        user_prompt = self.prompt.render("judge/user.j2", issue=issue)
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
        # Tracker tools write through a separate board instance.
        self.board.reload()
        render_all(self.issues_dir, self.board)
        _update_progress_from_judge(self.progress_path, iteration, issue, response)
        ctx.snapshot_workspace(f"iter-{iteration}-judge-{issue.id}-att{issue.attempts}")
        ctx.lprint(f">>> Judge verdict on #{issue.id}: {response.verdict.value.upper()}")
        return response

    def evaluate_performance(self, iteration: int) -> IssuePerfEvalResponse:
        ctx = self.ctx
        ctx.reselect_gpu()
        system_prompt = self.prompt.render(
            "perf_eval/system.j2",
            load_levels=self.load_levels,
            progress_path=None,
            perf_metrics_path=self.perf_metrics_location,
            issue_create_cap=self.max_issues_per_perf_eval,
            benchmark_command=ctx.judge_benchmark_command,
            runtime_notes=ctx.run_environment_view.prompt_notes,
        )
        user_prompt = self.prompt.render("perf_eval/user.j2")
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
        self.board.reload()
        render_all(self.issues_dir, self.board)
        _update_progress_from_perf_eval(self.progress_path, iteration, response)
        self.state_store.append_performance(
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


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def run_plain_loop(  # noqa: PLR0913  # tracked: #288
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
    integration: LocalRunIntegration | None = None,
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
    resolved_agent_backend = str(agent_backend or config.agent.backend or AgentBackend.CLI)
    options = PlainOrchestrationOptions(
        model=config.model.name,
        agent_backend=resolved_agent_backend,
        agent_driver=(
            resolve_agent_driver(config).value if resolved_agent_backend == "cli" else None
        ),
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
        legacy_configuration_factory=lambda resolved_profiler: legacy_configuration_from_options(
            options,
            run_environment=run_environment_record(run_environment),
            profiler=resolved_profiler.value,
        ),
        orchestration_descriptor=lambda resolved_profiler: descriptor_from_options(
            options, profiler=resolved_profiler.value
        ),
        orchestration_resume=compare_resume,
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
        # tracker access (an MCP server spec). The wrapper consumes an extra
        # ``iteration=`` kwarg on invoke() that the loop passes per call.
        # See vibesys/plain/runner_ext.py.
        ctx.agent_client = PlainLoopAgentClient(
            ctx.agent_client,
            max_issues_per_perf_eval=max_issues_per_perf_eval,
        )

        persisted_state = state_store.load_cursor()
        state = persisted_state or resume_state or PlainLoopState()
        effects = _PlainEffects(
            ctx=ctx,
            board=store,
            state_store=state_store,
            prompt=prompt,
            progress_path=progress_path,
            issues_dir=issues_dir,
            perf_metrics_location=perf_metrics_location,
            max_issues_per_perf_eval=max_issues_per_perf_eval,
            load_levels=config.perf_eval.load_levels,
        )
        return PlainPolicy(
            effects,
            state=state,
            max_rounds=max_rounds,
            max_attempts_per_issue=max_attempts_per_issue,
            resuming=existing or persisted_state is not None or resume_state is not None,
        ).run()
