"""Plain policy's issue-board setup and agent turn effects."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING, Any, Literal

from vibesys.loops.plain.render import render_all
from vibesys.loops.plain.state import PlainStateStore
from vibesys.prompts import PROMPTS_DIR, Prompt
from vibesys.render.sink import output_sink
from vibesys.run import LoopContext, RunStateNamespace
from vibesys.schemas import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    PerfMetrics,
    PerfTrend,
    Verdict,
)
from vs_agent.api import MCPServerSpec, RoundProgress
from vs_issue_board.api import (
    Issue,
    IssueBoard,
    IssueType,
)
from vs_loop_state.api import PlainLoopCursor, PlainPerformanceRecord

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "plain"
PlainPhase = Literal["implementer", "judge", "perf_eval"]
if TYPE_CHECKING:
    from collections.abc import Iterator

    from vibesys.config import LoadLevelCfg
    from vibesys.loops.plain.orchestration import PlainOrchestrationOptions
    from vibesys.orchestration.runtime import RunContext


def build_issue_mcp_spec(
    *,
    store_relpath: str,
    creator: str,
    iteration: int,
    cap: int | None,
    allowed_types: set[IssueType],
) -> MCPServerSpec:
    """Describe the issue-board MCP server and its per-phase policy."""
    args = [
        "-m",
        "vs_issue_board.mcp",
        store_relpath,
        "--creator",
        creator,
        "--iteration",
        str(iteration),
        "--allowed-types",
        ",".join(sorted(issue_type.value for issue_type in allowed_types)),
    ]
    if cap is not None:
        args += ["--cap", str(cap)]
    return MCPServerSpec(name="vibesys-issues", command="python", args=tuple(args))


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


@dataclass
class PlainRun:
    """Plain-owned state and effects bound to the one shared run host."""

    host: RunContext
    core: LoopContext
    board: IssueBoard
    state_store: PlainStateStore
    state: PlainLoopCursor
    turns: _PlainTurns
    resuming: bool
    prompt: Prompt
    options: PlainOrchestrationOptions

    @classmethod
    async def open(cls, host: RunContext, options: PlainOrchestrationOptions) -> PlainRun:
        """Open issue memory in the host's workspace and load its durable cursor."""
        core = host.run_context
        output_sink().run_configured(
            run_log_path=str(core.run_log_path),
            project_root=str(core.project_root),
            model=core.model_name,
        )
        prompt = Prompt(_TEMPLATE_DIR, core.backend)
        portable = core.state.portable(RunStateNamespace.PLAIN)
        state_store = PlainStateStore(portable)
        local_dir = core.state.local(RunStateNamespace.PLAIN).external_directory()
        progress_path = _init_progress(local_dir)
        issues_dir = local_dir / "issues"
        board: IssueBoard
        board = IssueBoard(
            core.workspace / "issues.json",
            on_change=lambda: render_all(issues_dir, board),
        )
        render_all(issues_dir, board)
        persisted = await host.state.load(PlainLoopCursor)
        turns = _PlainTurns(
            ctx=core,
            board=board,
            state_store=state_store,
            prompt=prompt,
            progress_path=progress_path,
            issues_dir=issues_dir,
            perf_metrics_location=portable.agent_visible_path("perf/metrics.json"),
            max_issues_per_perf_eval=options.max_issues_per_perf_eval,
            load_levels=host.request.config.perf_eval.load_levels,
        )
        return cls(
            host=host,
            core=core,
            board=board,
            state_store=state_store,
            state=persisted or PlainLoopCursor(),
            turns=turns,
            resuming=host.request.resume is not None or persisted is not None,
            prompt=prompt,
            options=options,
        )

    async def bootstrap(self) -> None:
        """Create the first candidate-facing issue and commit the cursor."""
        if self.state.bootstrap_done:
            return
        description = self.prompt.render(
            "bootstrap_issue.j2",
            reference_path=self.core.ref_name,
            accuracy_command=self.core.judge_accuracy_command,
            benchmark_command=self.core.judge_benchmark_command,
            runtime_notes=self.core.run_environment_view.prompt_notes,
        )
        issue = self.board.create(
            type=IssueType.FEATURE,
            title="Build FastAPI inference server for the reference model",
            description=description,
            created_by="loop:bootstrap",
            iteration=max(self.state.round_idx + 1, 1),
        )
        self.state = self.state.model_copy(update={"bootstrap_done": True})
        await self.checkpoint(
            self.state.round_idx,
            self.state.phase,
            self.state.current_issue_id,
            "plain: initialize issue board",
        )
        self.log(f"[bootstrap] created initial issue #{issue.id}")

    async def prepare_resume(self) -> None:
        """Restore the blocked queue only when a resumed run has budget."""
        if not self.resuming:
            return
        iteration = max(self.state.round_idx + 1, 1)
        if self.state.round_idx < self.options.max_rounds:
            reopened = self.board.reopen_blocked(
                actor="loop:resume", iteration=iteration, note="retried on resume"
            )
            if reopened:
                ids = ", ".join(f"#{issue_id}" for issue_id in reopened)
                self.log(f"[resume] reopened {len(reopened)} blocked issue(s): {ids}")
        self.log(
            f"Resuming at round {iteration} phase {self.state.phase!r}, "
            f"total limit {self.options.max_rounds}"
        )

    async def checkpoint(
        self, round_idx: int, phase: PlainPhase, issue_id: int | None, label: str
    ) -> None:
        """Commit the cursor and candidate worktree as one recoverable transition."""
        self.state = self.state.transition(
            round_idx=round_idx,
            phase=phase,
            current_issue_id=issue_id,
        )
        await self.host.state.checkpoint(self.state, sequence=round_idx + 1)
        self.log(f"[checkpoint] {label}")

    def performance_record(self, iteration: int) -> PlainPerformanceRecord | None:
        """Return a completed evaluation so a resume does not repeat its paid turn."""
        return next(
            (
                record
                for record in self.state_store.load_performance().records
                if record.iteration == iteration
            ),
            None,
        )

    @contextmanager
    def progress(self, iteration: int, total: int) -> Iterator[None]:
        """Scope one visible iteration without changing policy control flow."""
        progress = RoundProgress(iteration, total)
        self.log(f"\n{'=' * 60}\n  {progress.label()}\n{'=' * 60}\n")
        with self.core.progress(progress):
            yield

    def log(self, message: str) -> None:
        """Write a status line to the run log."""
        self.core.lprint(message)


@dataclass
class _PlainTurns:
    """Prompting, turns, and local artifacts for the plain policy."""

    ctx: LoopContext
    board: IssueBoard
    state_store: PlainStateStore
    prompt: Prompt
    progress_path: Path
    issues_dir: Path
    perf_metrics_location: str
    max_issues_per_perf_eval: int
    load_levels: list[LoadLevelCfg] | None

    def _issue_mcp_spec(
        self, *, creator: str, iteration: int, cap: int, allowed_types: set[IssueType]
    ) -> list[MCPServerSpec]:
        client = self.ctx.agent_client
        if not client.capabilities.mcp_servers:
            raise RuntimeError(  # noqa: TRY003
                f"agent backend {client.backend_name!r} cannot expose issue-board tools"
            )
        return [
            build_issue_mcp_spec(
                store_relpath="issues.json",
                creator=creator,
                iteration=iteration,
                cap=cap,
                allowed_types=allowed_types,
            )
        ]

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
            mcp_servers=self._issue_mcp_spec(
                creator="judge",
                iteration=iteration,
                cap=1,
                allowed_types={IssueType.BUG},
            ),
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
            mcp_servers=self._issue_mcp_spec(
                creator="perf_eval",
                iteration=iteration,
                cap=self.max_issues_per_perf_eval,
                allowed_types={IssueType.BUG, IssueType.FEATURE, IssueType.PERF},
            ),
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
        ctx.state.commit("plain: record performance evaluation", self.state_store.namespace)
        ctx.snapshot_workspace(f"iter-{iteration}-perf_eval")
        ctx.lprint(
            f"\n>>> Perf trend: throughput={response.throughput_trend.value.upper()}, "
            f"latency={response.latency_trend.value.upper()}"
        )
        return response
