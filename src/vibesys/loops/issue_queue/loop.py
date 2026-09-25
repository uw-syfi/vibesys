"""Plain policy's issue-board setup and agent turn effects."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING, Any, Literal

from vibesys.loops.issue_queue.render import render_all
from vibesys.loops.issue_queue.state import IssueQueueStateStore
from vibesys.orchestration.tools import mcp_spec_from_descriptor
from vibesys.prompts import PROMPTS_DIR, Prompt
from vibesys.schemas import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    PerfMetrics,
    PerfTrend,
    Verdict,
)
from vs_agent.api import MCPServerSpec, RoundProgress, expose_as_tools
from vs_issue_board.api import (
    Issue,
    IssueBoard,
    IssueType,
)
from vs_loop_state.api import PlainLoopCursor, PlainPerformanceRecord

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "issue_queue"
IssueQueuePhase = Literal["implementer", "judge", "perf_eval"]
if TYPE_CHECKING:
    from collections.abc import Iterator

    from vibesys.config import LoadLevelCfg
    from vibesys.loops.issue_queue.orchestration import IssueQueueOptions
    from vibesys.orchestration.runtime import RunContext
    from vibesys.runtime import AgentHandle


def build_issue_mcp_spec(
    *,
    store_relpath: str,
    creator: str,
    iteration: int,
    cap: int | None,
    allowed_types: set[IssueType],
) -> MCPServerSpec:
    """Describe the issue-board MCP server and its per-phase policy.

    Goes through the host's generic tool-serving descriptor
    (``vs_agent.expose_as_tools``) instead of hand-building an
    ``MCPServerSpec``; only the issue-board-specific argv stays here.
    """
    entrypoint_args = [
        store_relpath,
        "--creator",
        creator,
        "--iteration",
        str(iteration),
        "--allowed-types",
        ",".join(sorted(issue_type.value for issue_type in allowed_types)),
    ]
    if cap is not None:
        entrypoint_args += ["--cap", str(cap)]
    descriptor = expose_as_tools(
        name="vibesys-issues",
        entrypoint_module="vs_issue_board.mcp",
        entrypoint_args=tuple(entrypoint_args),
    )
    return mcp_spec_from_descriptor(descriptor)


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
class IssueQueueRun:
    """Plain-owned state and effects bound to the one shared run host."""

    host: RunContext
    board: IssueBoard
    state_store: IssueQueueStateStore
    state: PlainLoopCursor
    turns: _IssueQueueTurns
    resuming: bool
    prompt: Prompt
    options: IssueQueueOptions

    @classmethod
    async def open(cls, host: RunContext, options: IssueQueueOptions) -> IssueQueueRun:
        """Open issue memory in the host's workspace and load its durable cursor."""
        host.run_configured(
            run_log_path=str(host.environment.run_log_path),
            project_root=str(host.workspaces.root.path),
            model=host.environment.model_name,
        )
        prompt = Prompt(_TEMPLATE_DIR, host.request.backend)
        portable = host.state.namespace
        state_store = IssueQueueStateStore(portable)
        local_dir = host.state.local_namespace.external_directory()
        progress_path = _init_progress(local_dir)
        issues_dir = local_dir / "issues"
        board: IssueBoard
        board = IssueBoard(
            host.workspaces.root.path / "issues.json",
            on_change=lambda: render_all(issues_dir, board),
        )
        render_all(issues_dir, board)
        persisted = await host.state.slot("state.json", PlainLoopCursor).load()
        turns = _IssueQueueTurns(
            host=host,
            implementer=await host.agents.spawn(host.agents.default_definition("implementer")),
            judge_agent=await host.agents.spawn(host.agents.default_definition("judge")),
            perf_agent=await host.agents.spawn(host.agents.default_definition("perf_eval")),
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
            reference_path=self.host.environment.reference_path,
            accuracy_command=self.host.environment.view.paths.accuracy_command,
            benchmark_command=self.host.environment.view.paths.benchmark_command,
            runtime_notes=self.host.environment.view.prompt_notes,
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
        self, round_idx: int, phase: IssueQueuePhase, issue_id: int | None, label: str
    ) -> None:
        """Commit the cursor and candidate worktree as one recoverable transition."""
        self.state = self.state.transition(
            round_idx=round_idx,
            phase=phase,
            current_issue_id=issue_id,
        )
        await self.host.state.checkpoint(
            sequence=round_idx + 1,
            writes={
                "state.json": self.state,
                "perf/metrics.json": self.state_store.load_performance(),
            },
        )
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
        with self.host.agents.progress(progress):
            yield

    def log(self, message: str) -> None:
        """Write a status line to the run log."""
        self.host.log(message)


@dataclass
class _IssueQueueTurns:
    """Prompting, turns, and local artifacts for the plain policy."""

    host: RunContext
    implementer: AgentHandle
    judge_agent: AgentHandle
    perf_agent: AgentHandle
    board: IssueBoard
    state_store: IssueQueueStateStore
    prompt: Prompt
    progress_path: Path
    issues_dir: Path
    perf_metrics_location: str
    max_issues_per_perf_eval: int
    load_levels: list[LoadLevelCfg] | None

    def _issue_mcp_spec(
        self, *, creator: str, iteration: int, cap: int, allowed_types: set[IssueType]
    ) -> list[MCPServerSpec]:
        agent = self.judge_agent if creator == "judge" else self.perf_agent
        if not agent.capabilities.mcp_servers:
            raise RuntimeError(  # noqa: TRY003
                f"agent backend {agent.backend_name!r} cannot expose issue-board tools"
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

    async def implement(self, issue: Issue) -> IssueImplementerResponse:
        host = self.host
        await host.environment.reselect_device()
        system_prompt = self.prompt.render(
            "implementer/system.j2",
            reference_path=host.environment.reference_path,
            runtime_notes=host.environment.view.prompt_notes,
            issue=issue,
        )
        user_prompt = self.prompt.render(
            "implementer/user.j2",
            issue=issue,
            prior_judge_review=_latest_judge_review(issue),
        )
        await host.control.debug_step(f"Implementer step on issue #{issue.id}")
        host.log(f">>> Implementer working on issue #{issue.id}...")
        issue_id = issue.id
        return await self.implementer.turn_structured(
            user_prompt,
            system_prompt=system_prompt,
            response_cls=IssueImplementerResponse,
            fallback_factory=lambda: IssueImplementerResponse(
                issue_id=issue_id,
                summary="Implementer did not produce a structured response.",
                files_touched=[],
                self_check="No structured response received.",
            ),
            label=f"impl issue #{issue.id} att{issue.attempts + 1}",
        )

    async def record_implementation(
        self, issue: Issue, response: IssueImplementerResponse, iteration: int
    ) -> None:
        _update_progress_from_implementer(self.progress_path, iteration, issue, response)
        await self.host.workspaces.root.snapshot(
            f"iter-{iteration}-impl-{issue.id}-att{issue.attempts}"
        )
        self.host.log(f"[snapshot] iter-{iteration}-impl-{issue.id}-att{issue.attempts}")

    async def judge(self, issue: Issue, iteration: int) -> IssueJudgeResponse:
        host = self.host
        await host.environment.reselect_device()
        system_prompt = self.prompt.render(
            "judge/system.j2",
            accuracy_command=host.environment.view.paths.accuracy_command,
            benchmark_command=host.environment.view.paths.benchmark_command,
            issue=issue,
        )
        user_prompt = self.prompt.render("judge/user.j2", issue=issue)
        await host.control.debug_step(f"Judge step on issue #{issue.id}")
        host.log(f"\n>>> Judge reviewing issue #{issue.id}...")
        issue_id = issue.id
        response = await self.judge_agent.turn_structured(
            user_prompt,
            mcp_servers=self._issue_mcp_spec(
                creator="judge",
                iteration=iteration,
                cap=1,
                allowed_types={IssueType.BUG},
            ),
            system_prompt=system_prompt,
            response_cls=IssueJudgeResponse,
            fallback_factory=lambda: IssueJudgeResponse(
                issue_id=issue_id,
                analysis="No structured response received from judge.",
                feedback="Judge did not produce a structured response.",
                verdict=Verdict.FAIL,
                new_issues_filed=[],
            ),
            label=f"judge issue #{issue.id} att{issue.attempts}",
        )
        self.board.reload()
        render_all(self.issues_dir, self.board)
        _update_progress_from_judge(self.progress_path, iteration, issue, response)
        await host.workspaces.root.snapshot(
            f"iter-{iteration}-judge-{issue.id}-att{issue.attempts}"
        )
        host.log(f">>> Judge verdict on #{issue.id}: {response.verdict.value.upper()}")
        return response

    async def evaluate_performance(
        self, iteration: int, cursor: PlainLoopCursor
    ) -> IssuePerfEvalResponse:
        host = self.host
        await host.environment.reselect_device()
        system_prompt = self.prompt.render(
            "perf_eval/system.j2",
            load_levels=self.load_levels,
            progress_path=None,
            perf_metrics_path=self.perf_metrics_location,
            issue_create_cap=self.max_issues_per_perf_eval,
            benchmark_command=host.environment.view.paths.benchmark_command,
            runtime_notes=host.environment.view.prompt_notes,
        )
        user_prompt = self.prompt.render("perf_eval/user.j2")
        await host.control.debug_step("Perf evaluator step")
        host.log("\n>>> Performance Evaluator benchmarking...")
        response = await self.perf_agent.turn_structured(
            user_prompt,
            mcp_servers=self._issue_mcp_spec(
                creator="perf_eval",
                iteration=iteration,
                cap=self.max_issues_per_perf_eval,
                allowed_types={IssueType.BUG, IssueType.FEATURE, IssueType.PERF},
            ),
            system_prompt=system_prompt,
            response_cls=IssuePerfEvalResponse,
            fallback_factory=lambda: IssuePerfEvalResponse(
                analysis="No structured response received from perf evaluator.",
                metrics=PerfMetrics(load_levels=[]),
                evaluator_feedback=[],
                new_issue_ids=[],
                throughput_trend=PerfTrend.MIXED,
                latency_trend=PerfTrend.MIXED,
            ),
            label=f"perf_eval iter {iteration}",
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
        await host.state.checkpoint(
            sequence=cursor.round_idx + 1,
            writes={"state.json": cursor, "perf/metrics.json": self.state_store.load_performance()},
        )
        await host.workspaces.root.snapshot(f"iter-{iteration}-perf_eval")
        host.log(
            f"\n>>> Perf trend: throughput={response.throughput_trend.value.upper()}, "
            f"latency={response.latency_trend.value.upper()}"
        )
        return response
