"""Plain policy's issue-board setup and agent turn effects."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from io import StringIO
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from vibesys.loops.issue_queue.render import render_all
from vibesys.loops.issue_queue.state import IssueQueueStateStore
from vibesys.prompts import PROMPTS_DIR, Prompt
from vibesys.roles.implementer import ISSUE_IMPLEMENTER, IssueImplementerContext
from vibesys.roles.judge import ISSUE_JUDGE, IssueJudgeContext
from vibesys.roles.perf_eval import ISSUE_PERF_EVAL, IssuePerfEvalContext
from vs_agent.api import RoundProgress
from vs_issue_tracker.api import (
    CreateIssuePolicy,
    Issue,
    IssueTracker,
    IssueTrackerSession,
    IssueType,
    ProgressLog,
    open_issue_tracker_session,
)
from vs_loop_state.api import PlainLoopCursor, PlainPerformanceRecord

_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "issue_queue"
IssueQueuePhase = Literal["implementer", "judge", "perf_eval"]


def _bootstrap_issue_title(objective: str) -> str:
    """Summarize the task's first objective line for the initial issue title."""
    summary = next(
        (line.strip().lstrip("# ").strip() for line in objective.splitlines() if line.strip()),
        "",
    )
    for prefix in ("Objective -", "Objective:"):
        if summary.lower().startswith(prefix.lower()):
            summary = summary[len(prefix) :].strip()
            break
    if not summary:
        summary = "implement the task objective"
    return f"Initial task: {summary[:100]}"


class ImplementerUserContext(BaseModel):
    """Context for issue_queue's implementer ``user.j2`` (the turn's message text)."""

    model_config = ConfigDict(frozen=True)

    issue: Issue
    prior_judge_review: dict[str, Any] | None
    progress: str


class JudgeUserContext(BaseModel):
    """Context for issue_queue's judge ``user.j2``."""

    model_config = ConfigDict(frozen=True)

    issue: Issue
    progress: str


class BootstrapContext(BaseModel):
    """Context for issue_queue's ``bootstrap_issue.j2``."""

    model_config = ConfigDict(frozen=True)

    objective: str
    reference_path: str
    accuracy_command: str | None
    benchmark_command: str | None
    runtime_notes: str


if TYPE_CHECKING:
    from collections.abc import Iterator

    from vibesys.config import LoadLevelCfg
    from vibesys.evaluators.perf_reply import IssuePerfEvalResponse
    from vibesys.loops.issue_queue.orchestration import IssueQueueOptions
    from vibesys.orchestration.runtime import RunContext
    from vibesys.roles.implementer import IssueImplementerResponse
    from vibesys.roles.judge import IssueJudgeResponse
    from vibesys.runtime import AgentHandle


# ---------------------------------------------------------------------------
# Progress markdown helpers
# ---------------------------------------------------------------------------


def _format_progress_from_implementer(
    iteration: int,
    issue: Issue,
    response: IssueImplementerResponse,
) -> str:
    progress = StringIO()
    progress.write(f"## Iter {iteration} — Implementer on issue #{issue.id}\n\n")
    progress.write(f"**Issue**: [{issue.type.value}] {issue.title}\n\n")
    progress.write(f"**Summary**: {response.summary}\n\n")
    if response.files_touched:
        progress.write("**Files touched**:\n")
        for fp in response.files_touched:
            progress.write(f"- `{fp}`\n")
        progress.write("\n")
    progress.write(f"**Self-check**: {response.self_check}\n\n")
    return progress.getvalue()


def _format_progress_from_judge(
    iteration: int,
    issue: Issue,
    response: IssueJudgeResponse,
) -> str:
    progress = StringIO()
    progress.write(f"### Iter {iteration} — Judge on issue #{issue.id}\n\n")
    progress.write(f"**Verdict**: {response.verdict.value.upper()}\n\n")
    progress.write(f"**Analysis**: {response.analysis}\n\n")
    if response.feedback:
        progress.write(f"**Feedback**: {response.feedback}\n\n")
    if response.new_issues_filed:
        ids = ", ".join(f"#{i}" for i in response.new_issues_filed)
        progress.write(f"**New issues filed**: {ids}\n\n")
    return progress.getvalue()


def _format_progress_from_perf_eval(
    iteration: int,
    response: IssuePerfEvalResponse,
) -> str:
    progress = StringIO()
    progress.write(f"## Iter {iteration} — Performance Evaluator\n\n")
    progress.write(f"**Throughput trend**: {response.throughput_trend.value.upper()}\n\n")
    progress.write(f"**Latency trend**: {response.latency_trend.value.upper()}\n\n")
    progress.write(f"**Analysis**: {response.analysis}\n\n")
    if response.new_issue_ids:
        ids = ", ".join(f"#{i}" for i in response.new_issue_ids)
        progress.write(f"**New issues filed**: {ids}\n\n")
    if response.evaluator_feedback:
        progress.write("**Notes for next perf evaluator**:\n")
        for note in response.evaluator_feedback:
            progress.write(f"- {note}\n")
        progress.write("\n")
    return progress.getvalue()


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
    board: IssueTracker
    progress_log: ProgressLog
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
        issues_dir = local_dir / "issues"
        tracker_session = open_issue_tracker_session(
            options.tracker,
            local_store_path=host.workspaces.root.path / "issues.json",
            local_progress_path=local_dir / "progress.md",
            tool_store_path="issues.json",
            run_id=host.run_id,
            view_sink=lambda issues: render_all(issues_dir, issues),
        )
        board = tracker_session.tracker
        tracker_session.refresh()
        progress_log = tracker_session.progress
        persisted = await host.state.slot("state.json", PlainLoopCursor).load()
        turns = _IssueQueueTurns(
            host=host,
            implementer=await host.agents.spawn(host.agents.default_definition("implementer")),
            judge_agent=await host.agents.spawn(host.agents.default_definition("judge")),
            perf_agent=await host.agents.spawn(host.agents.default_definition("perf_eval")),
            board=board,
            tracker_session=tracker_session,
            state_store=state_store,
            prompt=prompt,
            progress_log=progress_log,
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
            progress_log=progress_log,
            resuming=host.request.resume is not None or persisted is not None,
            prompt=prompt,
            options=options,
        )

    async def bootstrap(self) -> None:
        """Create the first candidate-facing issue and commit the cursor."""
        if self.state.bootstrap_done:
            return
        objective = self.host.request.objective or self.host.request.input_bundle.objective
        context = BootstrapContext(
            objective=objective,
            reference_path=self.host.environment.reference_path,
            accuracy_command=self.host.environment.view.paths.accuracy_command,
            benchmark_command=self.host.environment.view.paths.benchmark_command,
            runtime_notes=self.host.environment.view.prompt_notes,
        )
        description = self.prompt.render("bootstrap_issue.j2", **context.model_dump())
        issue = self.board.create(
            type=IssueType.FEATURE,
            title=_bootstrap_issue_title(objective),
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
    board: IssueTracker
    tracker_session: IssueTrackerSession
    state_store: IssueQueueStateStore
    prompt: Prompt
    progress_log: ProgressLog
    perf_metrics_location: str
    max_issues_per_perf_eval: int
    load_levels: list[LoadLevelCfg] | None

    async def implement(self, issue: Issue) -> IssueImplementerResponse:
        host = self.host
        await host.environment.reselect_device()
        user_context = ImplementerUserContext(
            issue=issue,
            prior_judge_review=_latest_judge_review(issue),
            progress=self.progress_log.read().rstrip(),
        )
        user_prompt = self.prompt.render("implementer/user.j2", **user_context.model_dump())
        await host.control.debug_step(f"Implementer step on issue #{issue.id}")
        host.log(f">>> Implementer working on issue #{issue.id}...")
        reply = cast(
            "IssueImplementerResponse",
            await host.agents.turn(
                ISSUE_IMPLEMENTER,
                agent=self.implementer,
                context=IssueImplementerContext(
                    reference_path=host.environment.reference_path,
                    runtime_notes=host.environment.view.prompt_notes,
                    issue=issue,
                ),
                message=user_prompt,
                label=f"impl issue #{issue.id} att{issue.attempts + 1}",
                backend=host.request.backend,
            ),
        )
        return reply.model_copy(update={"issue_id": issue.id})

    async def record_implementation(
        self, issue: Issue, response: IssueImplementerResponse, iteration: int
    ) -> None:
        self.progress_log.append(_format_progress_from_implementer(iteration, issue, response))
        await self.host.workspaces.root.snapshot(
            f"iter-{iteration}-impl-{issue.id}-att{issue.attempts}"
        )
        self.host.log(f"[snapshot] iter-{iteration}-impl-{issue.id}-att{issue.attempts}")

    async def judge(self, issue: Issue, iteration: int) -> IssueJudgeResponse:
        host = self.host
        await host.environment.reselect_device()
        user_prompt = self.prompt.render(
            "judge/user.j2",
            **JudgeUserContext(
                issue=issue, progress=self.progress_log.read().rstrip()
            ).model_dump(),
        )
        await host.control.debug_step(f"Judge step on issue #{issue.id}")
        host.log(f"\n>>> Judge reviewing issue #{issue.id}...")
        reply = cast(
            "IssueJudgeResponse",
            await host.agents.turn(
                ISSUE_JUDGE,
                agent=self.judge_agent,
                context=IssueJudgeContext(
                    accuracy_command=host.environment.view.paths.accuracy_command,
                    benchmark_command=host.environment.view.paths.benchmark_command,
                    issue=issue,
                ),
                message=user_prompt,
                label=f"judge issue #{issue.id} att{issue.attempts}",
                tool_servers=[
                    self.tracker_session.issue_tool_server(
                        CreateIssuePolicy(
                            creator="judge",
                            iteration=iteration,
                            cap=1,
                            allowed_types=frozenset({IssueType.BUG}),
                        )
                    )
                ],
                backend=host.request.backend,
            ),
        )
        response = reply.model_copy(update={"issue_id": issue.id})
        self.tracker_session.refresh()
        self.progress_log.append(_format_progress_from_judge(iteration, issue, response))
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
        user_prompt = self.prompt.render("perf_eval/user.j2")
        await host.control.debug_step("Perf evaluator step")
        host.log("\n>>> Performance Evaluator benchmarking...")
        response = cast(
            "IssuePerfEvalResponse",
            await host.agents.turn(
                ISSUE_PERF_EVAL,
                agent=self.perf_agent,
                context=IssuePerfEvalContext(
                    load_levels=self.load_levels,
                    perf_metrics_path=self.perf_metrics_location,
                    issue_create_cap=self.max_issues_per_perf_eval,
                    benchmark_command=host.environment.view.paths.benchmark_command,
                    runtime_notes=host.environment.view.prompt_notes,
                ),
                message=user_prompt,
                label=f"perf_eval iter {iteration}",
                tool_servers=[
                    self.tracker_session.issue_tool_server(
                        CreateIssuePolicy(
                            creator="perf_eval",
                            iteration=iteration,
                            cap=self.max_issues_per_perf_eval,
                            allowed_types=frozenset(
                                {IssueType.BUG, IssueType.FEATURE, IssueType.PERF}
                            ),
                        )
                    )
                ],
                backend=host.request.backend,
            ),
        )
        self.tracker_session.refresh()
        self.progress_log.append(_format_progress_from_perf_eval(iteration, response))
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
