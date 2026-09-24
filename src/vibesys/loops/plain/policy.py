"""Issue-drain orchestration independent of agents and run infrastructure."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from vibesys.schemas import Verdict
from vs_issue_board.api import IssueStatus

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from vibesys.schemas import IssueImplementerResponse, IssueJudgeResponse, IssuePerfEvalResponse
    from vs_issue_board.api import Issue
    from vs_loop_state.api import PlainLoopCursor

PlainPhase = Literal["implementer", "judge", "perf_eval"]


class PlainPolicyPort(Protocol):
    """Typed effects required by the issue-drain policy."""

    def bootstrap(self, state: PlainLoopCursor) -> None:
        """Create the first issue if the persisted cursor requires it."""
        ...

    def checkpoint(self, state: PlainLoopCursor, label: str) -> None:
        """Persist the cursor and commit the recovery point."""
        ...

    def progress(self, iteration: int, total: int) -> AbstractContextManager[None]:
        """Scope one iteration's progress reporting."""
        ...

    def log(self, message: str) -> None:
        """Record a policy status message."""
        ...

    def get_issue(self, issue_id: int) -> Issue | None:
        """Read an issue by ID."""
        ...

    def next_open_issue(self) -> Issue | None:
        """Choose the next open issue."""
        ...

    def list_issues(self, status: IssueStatus | None = None) -> list[Issue]:
        """Read issues, optionally filtered by status."""
        ...

    def reopen_blocked(self, iteration: int) -> list[int]:
        """Reset previously blocked issues for a resumed run."""
        ...

    def claim(self, issue: Issue, iteration: int) -> Issue:
        """Mark an open issue in progress."""
        ...

    def block(self, issue: Issue, iteration: int, max_attempts: int) -> None:
        """Record that an issue exhausted its attempt budget."""
        ...

    def increment_attempts(
        self, issue: Issue, response: IssueImplementerResponse, iteration: int
    ) -> Issue:
        """Persist an implementer attempt."""
        ...

    def close_issue(self, issue: Issue, response: IssueJudgeResponse, iteration: int) -> None:
        """Record a passing judge verdict."""
        ...

    def reopen_issue(self, issue: Issue, response: IssueJudgeResponse, iteration: int) -> None:
        """Record a failing judge verdict."""
        ...

    def implement(self, issue: Issue) -> IssueImplementerResponse:
        """Run one implementer turn."""
        ...

    def record_implementation(
        self, issue: Issue, response: IssueImplementerResponse, iteration: int
    ) -> None:
        """Write progress and snapshot the attempt."""
        ...

    def judge(self, issue: Issue, iteration: int) -> IssueJudgeResponse:
        """Run one judge turn and capture its workspace."""
        ...

    def evaluate_performance(self, iteration: int) -> IssuePerfEvalResponse:
        """Run the performance evaluator and persist its metrics."""
        ...


def _resume_point(
    state: PlainLoopCursor, get_issue: Callable[[int], Issue | None]
) -> tuple[int, str, int | None]:
    """Revisit an interrupted role only while its issue remains actionable."""
    issue_id = state.current_issue_id
    if issue_id is not None and state.phase in {"judge", "implementer"}:
        issue = get_issue(issue_id)
        if issue is not None and issue.status in (IssueStatus.IN_PROGRESS, IssueStatus.OPEN):
            return state.round_idx, state.phase, issue_id
    return state.round_idx, "implementer", None


class PlainPolicy:
    """Own issue retries, role handoffs, checkpoints, and termination."""

    def __init__(
        self,
        port: PlainPolicyPort,
        *,
        state: PlainLoopCursor,
        max_rounds: int,
        max_attempts_per_issue: int,
        resuming: bool,
    ) -> None:
        """Bind persisted cursor and budgets to effects for one run."""
        self.port = port
        self.state = state
        self.max_rounds = max_rounds
        self.max_attempts_per_issue = max_attempts_per_issue
        self.resuming = resuming

    def _checkpoint(
        self, round_idx: int, phase: PlainPhase, issue_id: int | None, label: str
    ) -> None:
        self.state = self.state.transition(
            round_idx=round_idx, phase=phase, current_issue_id=issue_id
        )
        self.port.checkpoint(self.state, label)

    def _process_issue(
        self, issue: Issue, *, round_idx: int, iteration: int, resume_judge: bool
    ) -> None:
        port = self.port
        if issue.status == IssueStatus.OPEN:
            issue = port.claim(issue, iteration)
        if not resume_judge:
            self._checkpoint(
                round_idx,
                "implementer",
                issue.id,
                f"plain: begin implementer for issue {issue.id}",
            )
            response = port.implement(issue)
            issue = port.increment_attempts(issue, response, iteration)
            port.record_implementation(issue, response, iteration)
        self._checkpoint(round_idx, "judge", issue.id, f"plain: begin judge for issue {issue.id}")
        verdict = port.judge(issue, iteration)
        if verdict.verdict == Verdict.PASS:
            port.close_issue(issue, verdict, iteration)
        else:
            port.reopen_issue(issue, verdict, iteration)
        self._checkpoint(
            round_idx, "implementer", None, f"plain: record judge result for issue {issue.id}"
        )

    def _drain(
        self, *, round_idx: int, iteration: int, pending_issue_id: int | None, next_phase: str
    ) -> None:
        port = self.port
        while True:
            issue = (
                port.get_issue(pending_issue_id)
                if pending_issue_id is not None
                else port.next_open_issue()
            )
            pending_issue_id = None
            if issue is None:
                return
            if issue.attempts >= self.max_attempts_per_issue:
                port.block(issue, iteration, self.max_attempts_per_issue)
                port.log(f"[block] issue #{issue.id} blocked after {issue.attempts} attempts")
                continue
            self._process_issue(
                issue,
                round_idx=round_idx,
                iteration=iteration,
                resume_judge=next_phase == "judge",
            )
            next_phase = ""

    def _evaluate(self, *, round_idx: int, iteration: int) -> bool | None:
        port = self.port
        remaining = [issue for issue in port.list_issues() if issue.status != IssueStatus.CLOSED]
        if remaining and all(issue.status == IssueStatus.BLOCKED for issue in remaining):
            port.log(
                f"[stop] all remaining issues are blocked ({len(remaining)} blocked); bailing out."
            )
            self._checkpoint(round_idx, "perf_eval", None, "plain: record blocked issue queue")
            return False
        self._checkpoint(
            round_idx, "perf_eval", None, f"plain: begin performance evaluation {iteration}"
        )
        response = port.evaluate_performance(iteration)
        self._checkpoint(
            round_idx, "perf_eval", None, f"plain: record performance evaluation {iteration}"
        )
        if not port.list_issues(IssueStatus.OPEN) and not response.new_issue_ids:
            port.log("[done] no open issues and perf_eval filed none.")
            self._checkpoint(
                round_idx + 1,
                "implementer",
                None,
                f"plain: complete performance evaluation {iteration}",
            )
            return True
        self._checkpoint(round_idx + 1, "implementer", None, f"plain: complete round {iteration}")
        return None

    def run(self) -> bool:
        """Run issue drain and performance rounds through the injected effects."""
        if not self.state.bootstrap_done:
            self.port.bootstrap(self.state)
        if self.resuming:
            reopened = self.port.reopen_blocked(max(self.state.round_idx + 1, 1))
            if reopened:
                ids = ", ".join(f"#{issue_id}" for issue_id in reopened)
                self.port.log(
                    f"[resume] reopened {len(reopened)} previously blocked issue(s) "
                    f"for retry: {ids}"
                )
        round_idx, next_phase, pending_issue_id = _resume_point(self.state, self.port.get_issue)
        end_iteration = round_idx + self.max_rounds
        if self.resuming:
            self.port.log(
                f"Resuming at round {round_idx + 1} phase '{next_phase}'"
                + (f" issue #{pending_issue_id}" if pending_issue_id else "")
                + f", running up to {self.max_rounds} more rounds"
            )
        while round_idx < end_iteration:
            iteration = round_idx + 1
            with self.port.progress(iteration, end_iteration):
                self._drain(
                    round_idx=round_idx,
                    iteration=iteration,
                    pending_issue_id=pending_issue_id,
                    next_phase=next_phase,
                )
                outcome = self._evaluate(round_idx=round_idx, iteration=iteration)
                if outcome is not None:
                    return outcome
            pending_issue_id = None
            next_phase = ""
            round_idx += 1
        self.port.log("Run completed — round budget exhausted.")
        return False
