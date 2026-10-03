"""Issue-driven orchestration over explicit runtime capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.orchestration.issue_queue.agents import IMPLEMENTER, JUDGE, PERF_EVALUATOR
from vibesys.orchestration.issue_queue.artifacts import append_progress, render_all
from vibesys.orchestration.issue_queue.models import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    IssueQueueOptions,
    IssueQueuePhase,
    IssueQueueState,
    IssueToolPolicy,
    PerformanceRecord,
    latest_judge_review,
)
from vibesys.orchestration.issue_queue.prompts import (
    bootstrap_description,
    implementer_message,
    judge_message,
    performance_message,
)
from vibesys.orchestration.structured_turn import structured_turn
from vs_issue_tracker.api import (
    Issue,
    IssueStatus,
    IssueTracker,
    IssueTrackerSession,
    IssueType,
    ProgressLog,
    open_issue_tracker_session,
)
from vs_runtime.api import AgentSession, Run, RunStatus

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

_ISSUES_FILE = "issues.json"
_ISSUES_DIRECTORY = ".vibesys/issues"
_PROGRESS_FILE = "progress.md"
_TOOL_POLICY_FILE = ".vibesys/issue-tool-policy.json"
_TRACKER_CONFIG_FILE = ".vibesys/issue-tracker.json"


def _write_model(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


@dataclass(slots=True)
class _Sessions:
    implementer: AgentSession
    judge: AgentSession
    performance: AgentSession

    async def close(self) -> None:
        """Close long-lived conversations in reverse creation order."""
        await self.performance.close()
        await self.judge.close()
        await self.implementer.close()


class _IssueQueueRun:
    """One run's strict state, policy artifacts, and long-lived sessions."""

    def __init__(self, run: Run, options: IssueQueueOptions) -> None:
        self.run = run
        self.options = options
        self.workspace = run.workspaces.root
        self.root = self.workspace.path
        self._tracker_session: IssueTrackerSession | None = None
        self.state = IssueQueueState()
        self.sessions: _Sessions | None = None
        self.resuming = False

    async def initialize(self) -> None:
        """Acquire all sessions before creating or changing policy artifacts."""
        loaded = await self.run.state.load(IssueQueueState)
        self.resuming = loaded is not None
        self.state = loaded or IssueQueueState()
        implementer = await self.run.agents.create_session(
            IMPLEMENTER,
            workspace=self.workspace,
            member_id="issue-queue-implementer",
        )
        judge = await self.run.agents.create_session(
            JUDGE,
            workspace=self.workspace,
            member_id="issue-queue-judge",
        )
        performance = await self.run.agents.create_session(
            PERF_EVALUATOR,
            workspace=self.workspace,
            member_id="issue-queue-perf-evaluator",
        )
        self.sessions = _Sessions(implementer, judge, performance)
        _write_model(self.root / _TRACKER_CONFIG_FILE, self.options.tracker)
        self._tracker_session = open_issue_tracker_session(
            self.options.tracker,
            local_store_path=self.root / _ISSUES_FILE,
            local_progress_path=self.root / _PROGRESS_FILE,
            run_id=self.run.run_id,
            view_sink=lambda issues: render_all(self.root / _ISSUES_DIRECTORY, issues),
        )
        self.tracker_session.refresh()

        if not self.state.bootstrap_done:
            self.board.create(
                type=IssueType.FEATURE,
                title="Build inference server for the reference model",
                description=bootstrap_description(self.run.facts),
                created_by="loop:bootstrap",
                iteration=max(self.state.round_idx + 1, 1),
            )
            self.state = self.state.mark_bootstrapped()
            await self.commit("issue_queue: initialize issue board")
        elif self.state.round_idx < self.options.max_rounds:
            reopened = self.board.reopen_blocked(
                actor="loop:resume",
                iteration=max(self.state.round_idx + 1, 1),
                note="retried on resume",
            )
            if reopened:
                issue_ids = ", ".join(f"#{issue_id}" for issue_id in reopened)
                self.run.observations.note(f"[resume] reopened blocked issues: {issue_ids}")
                await self.commit("issue_queue: reopen blocked issues")

    async def close(self) -> None:
        """Release plugin-owned handles; the runtime remains their final owner."""
        if self.sessions is not None:
            await self.sessions.close()

    @property
    def active_sessions(self) -> _Sessions:
        """Return initialized role conversations."""
        if self.sessions is None:
            message = "issue-queue sessions are not initialized"
            raise RuntimeError(message)
        return self.sessions

    @property
    def tracker_session(self) -> IssueTrackerSession:
        """Return the storage-neutral tracker resources for this run."""
        if self._tracker_session is None:
            message = "issue queue is not initialized"
            raise RuntimeError(message)
        return self._tracker_session

    @property
    def board(self) -> IssueTracker:
        """Return the issue tracker after session acquisition succeeds."""
        return self.tracker_session.tracker

    @property
    def progress(self) -> ProgressLog:
        """Return the run's storage-neutral progress log."""
        return self.tracker_session.progress

    async def commit(self, label: str) -> None:
        """Persist the aggregate and its policy artifacts with the root workspace."""
        self.tracker_session.refresh()
        await self.run.state.commit(self.state, workspace=self.workspace, label=label)

    async def transition(
        self,
        *,
        round_idx: int,
        phase: IssueQueuePhase,
        issue_id: int | None,
        label: str,
    ) -> None:
        self.state = self.state.transition(
            round_idx=round_idx,
            phase=phase,
            current_issue_id=issue_id,
        )
        await self.commit(label)

    def set_tool_policy(self, value: IssueToolPolicy) -> None:
        """Publish the per-turn authorization read by the fixed tool server."""
        _write_model(self.root / _TOOL_POLICY_FILE, value)

    def performance_record(self, iteration: int) -> PerformanceRecord | None:
        return next(
            (record for record in self.state.performance if record.iteration == iteration),
            None,
        )


def _resume_point(run: _IssueQueueRun) -> tuple[int, IssueQueuePhase, int | None]:
    state = run.state
    issue_id = state.current_issue_id
    if issue_id is not None and state.phase in {"implementer", "judge"}:
        issue = run.board.get(issue_id)
        if issue is not None and issue.status in {IssueStatus.OPEN, IssueStatus.IN_PROGRESS}:
            return state.round_idx, state.phase, issue_id
    return state.round_idx, "implementer", None


async def _implement(run: _IssueQueueRun, issue: Issue, iteration: int) -> Issue:
    response = await structured_turn(
        run.active_sessions.implementer,
        implementer_message(issue, run.run.facts, latest_judge_review(issue)),
        IssueImplementerResponse,
    )
    response = response.model_copy(update={"issue_id": issue.id})
    updated = run.board.increment_attempts(
        issue.id,
        actor="implementer",
        iteration=iteration,
        note=response.summary[:200],
        payload=response.model_dump(mode="json"),
    )
    append_progress(
        run.progress, response, iteration=iteration, step="implement", issue_id=issue.id
    )
    return updated


async def _judge(run: _IssueQueueRun, issue: Issue, iteration: int) -> IssueJudgeResponse:
    run.set_tool_policy(
        IssueToolPolicy(
            creator="judge",
            iteration=iteration,
            cap=1,
            allowed_types=("bug",),
        )
    )
    response = await structured_turn(
        run.active_sessions.judge,
        judge_message(issue, run.run.facts),
        IssueJudgeResponse,
    )
    response = response.model_copy(update={"issue_id": issue.id})
    run.tracker_session.refresh()
    status = IssueStatus.CLOSED if response.verdict == "pass" else IssueStatus.OPEN
    note = (
        f"closed by judge after attempt {issue.attempts}"
        if response.verdict == "pass"
        else response.feedback[:500]
    )
    run.board.update_status(
        issue.id,
        status,
        actor="judge",
        iteration=iteration,
        note=note,
        payload=response.model_dump(mode="json"),
    )
    append_progress(run.progress, response, iteration=iteration, step="review", issue_id=issue.id)
    return response


async def _process_issue(
    run: _IssueQueueRun,
    issue: Issue,
    *,
    round_idx: int,
    iteration: int,
    resume_judge: bool,
) -> None:
    if issue.status is IssueStatus.OPEN:
        issue = run.board.update_status(
            issue.id,
            IssueStatus.IN_PROGRESS,
            actor="loop",
            iteration=iteration,
            note="claimed for processing",
        )
    if not resume_judge:
        await run.transition(
            round_idx=round_idx,
            phase="implementer",
            issue_id=issue.id,
            label=f"issue_queue: begin implementer for issue {issue.id}",
        )
        issue = await _implement(run, issue, iteration)
    await run.transition(
        round_idx=round_idx,
        phase="judge",
        issue_id=issue.id,
        label=f"issue_queue: begin judge for issue {issue.id}",
    )
    await _judge(run, issue, iteration)
    await run.transition(
        round_idx=round_idx,
        phase="implementer",
        issue_id=None,
        label=f"issue_queue: record judge result for issue {issue.id}",
    )


async def _drain(
    run: _IssueQueueRun,
    *,
    round_idx: int,
    iteration: int,
    next_phase: IssueQueuePhase,
    pending_issue_id: int | None,
) -> None:
    while issue := (
        run.board.get(pending_issue_id) if pending_issue_id is not None else run.board.next_open()
    ):
        resume_judge = next_phase == "judge"
        pending_issue_id, next_phase = None, "implementer"
        if issue.attempts >= run.options.max_attempts_per_issue and not resume_judge:
            run.board.update_status(
                issue.id,
                IssueStatus.BLOCKED,
                actor="loop",
                iteration=iteration,
                note=f"exhausted {run.options.max_attempts_per_issue} attempts",
            )
            await run.commit(f"issue_queue: block issue {issue.id}")
            continue
        await _process_issue(
            run,
            issue,
            round_idx=round_idx,
            iteration=iteration,
            resume_judge=resume_judge,
        )


async def _performance(run: _IssueQueueRun, round_idx: int, iteration: int) -> bool | None:
    remaining = [issue for issue in run.board.list() if issue.status is not IssueStatus.CLOSED]
    if remaining and all(issue.status is IssueStatus.BLOCKED for issue in remaining):
        run.run.observations.note(f"[stop] all {len(remaining)} remaining issues are blocked")
        await run.transition(
            round_idx=round_idx,
            phase="perf_eval",
            issue_id=None,
            label="issue_queue: record blocked queue",
        )
        return False

    await run.transition(
        round_idx=round_idx,
        phase="perf_eval",
        issue_id=None,
        label=f"issue_queue: begin performance evaluation {iteration}",
    )
    recorded = run.performance_record(iteration)
    if recorded is None:
        run.set_tool_policy(
            IssueToolPolicy(
                creator="perf_eval",
                iteration=iteration,
                cap=run.options.max_issues_per_perf_eval,
                allowed_types=("bug", "feature", "perf"),
            )
        )
        response = await structured_turn(
            run.active_sessions.performance,
            performance_message(
                iteration=iteration,
                facts=run.run.facts,
                options=run.options,
                state=run.state,
            ),
            IssuePerfEvalResponse,
        )
        run.tracker_session.refresh()
        recorded = PerformanceRecord(
            iteration=iteration,
            throughput_trend=response.throughput_trend,
            latency_trend=response.latency_trend,
            metrics=response.metrics.model_dump(mode="json"),
            new_issue_ids=response.new_issue_ids,
        )
        run.state = run.state.append_performance(recorded)
        append_progress(run.progress, response, iteration=iteration, step="performance")
        await run.commit(f"issue_queue: record performance evaluation {iteration}")

    if not run.board.list(status=IssueStatus.OPEN) and not recorded.new_issue_ids:
        await run.transition(
            round_idx=round_idx + 1,
            phase="implementer",
            issue_id=None,
            label=f"issue_queue: complete performance evaluation {iteration}",
        )
        return True
    await run.transition(
        round_idx=round_idx + 1,
        phase="implementer",
        issue_id=None,
        label=f"issue_queue: complete round {iteration}",
    )
    return None


async def orchestrate(run: Run, raw_options: object) -> RunStatus:
    """Drain the issue queue and benchmark after each bounded pass."""
    options = IssueQueueOptions.model_validate(raw_options)
    loop = _IssueQueueRun(run, options)
    try:
        await loop.initialize()
        round_idx, next_phase, pending_issue_id = _resume_point(loop)
        while round_idx < options.max_rounds:
            await run.control.checkpoint()
            iteration = round_idx + 1
            run.observations.note(f"round {iteration}/{options.max_rounds}")
            await _drain(
                loop,
                round_idx=round_idx,
                iteration=iteration,
                next_phase=next_phase,
                pending_issue_id=pending_issue_id,
            )
            result = await _performance(loop, round_idx, iteration)
            if result is not None:
                return RunStatus.SUCCEEDED if result else RunStatus.FAILED
            round_idx += 1
            next_phase, pending_issue_id = "implementer", None
        run.observations.note("run completed: round budget exhausted")
        return RunStatus.FAILED
    finally:
        await loop.close()


__all__ = ["orchestrate"]
