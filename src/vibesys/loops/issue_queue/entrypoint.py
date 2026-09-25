"""The issue-board orchestrator and its committed-state projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vibesys.context import RunSetup, RunStartHints
from vibesys.loops.issue_queue.loop import IssueQueueRun
from vibesys.loops.issue_queue.orchestration import compare_resume, options_from_descriptor
from vibesys.orchestration.view import RunStatus, RunView
from vibesys.schemas import Verdict
from vs_issue_board.api import Issue, IssueStatus
from vs_loop_state.api import PlainLoopCursor, PlainPerformanceSnapshot

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.runtime import RunContext
    from vs_issue_board.api import IssueBoard
    from vs_project.api import OrchestrationDescriptor, Project


def resume_point(state: PlainLoopCursor, board: IssueBoard) -> tuple[int, str, int | None]:
    """Revisit an interrupted role only while its issue remains actionable."""
    issue_id = state.current_issue_id
    if issue_id is not None and state.phase in {"judge", "implementer"}:
        issue = board.get(issue_id)
        if issue is not None and issue.status in (IssueStatus.IN_PROGRESS, IssueStatus.OPEN):
            return state.round_idx, state.phase, issue_id
    return state.round_idx, "implementer", None


class IssueQueueOrchestrator:
    """Drain issues, review each attempt, and evaluate the resulting candidate."""

    def __init__(self, descriptor: OrchestrationDescriptor) -> None:
        """Validate plain options before host setup."""
        self.options = options_from_descriptor(descriptor)
        self.setup = RunSetup(
            state_namespace="plain",
            state_slots={
                "state.json": PlainLoopCursor,
                "perf/metrics.json": PlainPerformanceSnapshot,
            },
            resume_policy=compare_resume,
            start_hints=RunStartHints(
                max_rounds=self.options.max_rounds,
                expected_roles=("implementer", "judge", "perf_eval"),
            ),
        )

    async def run(self, ctx: RunContext) -> bool:
        """Run one total round budget through the shared host."""
        run = await IssueQueueRun.open(ctx, self.options)
        await run.bootstrap()
        await run.prepare_resume()
        round_idx, next_phase, pending_issue_id = resume_point(run.state, run.board)

        while round_idx < self.options.max_rounds:
            await ctx.control.boundary()
            iteration = round_idx + 1
            with run.progress(iteration, self.options.max_rounds):
                await self._drain(run, round_idx, iteration, next_phase, pending_issue_id)
                result = await self._evaluate(run, round_idx, iteration)
                if result is not None:
                    return result
            round_idx += 1
            next_phase, pending_issue_id = "implementer", None
        run.log("Run completed: round budget exhausted.")
        return False

    async def _drain(
        self,
        run: IssueQueueRun,
        round_idx: int,
        iteration: int,
        next_phase: str,
        pending_issue_id: int | None,
    ) -> None:
        while issue := (
            run.board.get(pending_issue_id)
            if pending_issue_id is not None
            else run.board.next_open()
        ):
            resume_judge = next_phase == "judge"
            pending_issue_id, next_phase = None, "implementer"
            if issue.attempts >= self.options.max_attempts_per_issue and not resume_judge:
                run.board.update_status(
                    issue.id,
                    IssueStatus.BLOCKED,
                    actor="loop",
                    iteration=iteration,
                    note=f"exhausted {self.options.max_attempts_per_issue} attempts",
                )
                run.log(f"[block] issue #{issue.id} blocked after {issue.attempts} attempts")
                continue
            await self._process_issue(run, issue, round_idx, iteration, resume_judge=resume_judge)

    async def _process_issue(
        self,
        run: IssueQueueRun,
        issue: Issue,
        round_idx: int,
        iteration: int,
        *,
        resume_judge: bool,
    ) -> None:
        if issue.status == IssueStatus.OPEN:
            issue = run.board.update_status(
                issue.id,
                IssueStatus.IN_PROGRESS,
                actor="loop",
                iteration=iteration,
                note="claimed for processing",
            )
        if not resume_judge:
            await run.checkpoint(
                round_idx,
                "implementer",
                issue.id,
                f"plain: begin implementer for issue {issue.id}",
            )
            response = await run.turns.implement(issue)
            issue = run.board.increment_attempts(
                issue.id,
                actor="implementer",
                iteration=iteration,
                note=response.summary[:200],
                payload=response.model_dump(mode="json"),
            )
            await run.turns.record_implementation(issue, response, iteration)

        await run.checkpoint(
            round_idx, "judge", issue.id, f"plain: begin judge for issue {issue.id}"
        )
        verdict = await run.turns.judge(issue, iteration)
        if verdict.verdict == Verdict.PASS:
            run.board.update_status(
                issue.id,
                IssueStatus.CLOSED,
                actor="judge",
                iteration=iteration,
                note=f"closed by judge after attempt {issue.attempts}",
                payload=verdict.model_dump(mode="json"),
            )
        else:
            run.board.update_status(
                issue.id,
                IssueStatus.OPEN,
                actor="judge",
                iteration=iteration,
                note=verdict.feedback[:500],
                payload=verdict.model_dump(mode="json"),
            )
        await run.checkpoint(
            round_idx,
            "implementer",
            None,
            f"plain: record judge result for issue {issue.id}",
        )

    async def _evaluate(self, run: IssueQueueRun, round_idx: int, iteration: int) -> bool | None:
        remaining = [issue for issue in run.board.list() if issue.status != IssueStatus.CLOSED]
        if remaining and all(issue.status == IssueStatus.BLOCKED for issue in remaining):
            run.log(f"[stop] all remaining issues are blocked ({len(remaining)} blocked).")
            await run.checkpoint(round_idx, "perf_eval", None, "plain: record blocked queue")
            return False

        await run.checkpoint(
            round_idx,
            "perf_eval",
            None,
            f"plain: begin performance evaluation {iteration}",
        )
        recorded = run.performance_record(iteration)
        if recorded is None:
            response = await run.turns.evaluate_performance(iteration, run.state)
            new_issue_ids = response.new_issue_ids
        else:
            new_issue_ids = recorded.new_issue_ids
        await run.checkpoint(
            round_idx,
            "perf_eval",
            None,
            f"plain: record performance evaluation {iteration}",
        )
        if not run.board.list(status=IssueStatus.OPEN) and not new_issue_ids:
            run.log("[done] no open issues and perf_eval filed none.")
            await run.checkpoint(
                round_idx + 1,
                "implementer",
                None,
                f"plain: complete performance evaluation {iteration}",
            )
            return True
        await run.checkpoint(
            round_idx + 1,
            "implementer",
            None,
            f"plain: complete round {iteration}",
        )
        return None


@dataclass(frozen=True, slots=True)
class IssueQueueProjector:
    """Expose the same committed cursor for live and historical readers."""

    def view(self, project: Project, run_id: str, *, status: RunStatus, loop: str) -> RunView:
        """Read the committed cursor from the plain portable namespace."""
        slot = project.state.portable_namespace(run_id, "plain").slot("state.json", PlainLoopCursor)
        state = slot.load_optional() or PlainLoopCursor()
        return self._view(state, run_id=run_id, status=status, loop=loop)

    def project_committed(self, namespace: str, state: BaseModel, *, run_id: str) -> RunView | None:
        """Project an in-memory cursor only after its host checkpoint commits."""
        if namespace != "plain" or not isinstance(state, PlainLoopCursor):
            return None
        return self._view(state, run_id=run_id, status=RunStatus.ACTIVE, loop="plain")

    @staticmethod
    def _view(state: PlainLoopCursor, *, run_id: str, status: RunStatus, loop: str) -> RunView:
        return RunView(
            run_id=run_id,
            loop=loop,
            status=status,
            projection=state.model_dump(mode="json"),
        )
