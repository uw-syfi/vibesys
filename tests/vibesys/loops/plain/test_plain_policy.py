"""Plain orchestration decisions with in-memory effects only."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from vibesys.loops.plain.policy import PlainPolicy
from vibesys.schemas import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    PerfMetrics,
    PerfTrend,
    Verdict,
)
from vs_issue_board.api import Issue, IssueStatus, IssueType
from vs_loop_state.api import PlainLoopCursor

if TYPE_CHECKING:
    from collections.abc import Iterator


def _issue(issue_id: int, *, status: IssueStatus = IssueStatus.OPEN, attempts: int = 0) -> Issue:
    return Issue(
        id=issue_id,
        type=IssueType.FEATURE,
        title=f"Issue {issue_id}",
        description="Improve the candidate.",
        status=status,
        created_by="test",
        created_iter=1,
        created_at="2026-01-01",
        updated_at="2026-01-01",
        attempts=attempts,
    )


class _FakePlainPort:
    def __init__(
        self, issues: list[Issue], verdicts: list[Verdict], perf_issues: list[list[int]]
    ) -> None:
        self.issues = {issue.id: issue for issue in issues}
        self.verdicts = iter(verdicts)
        self.perf_issues = iter(perf_issues)
        self.calls: list[str] = []
        self.checkpoints: list[tuple[str, PlainLoopCursor]] = []

    def bootstrap(self, state: PlainLoopCursor) -> None:
        self.calls.append("bootstrap")
        if not state.bootstrap_done:
            self.issues[1] = _issue(1)
            state.bootstrap_done = True
            self.checkpoint(state, "plain: initialize issue board")

    def checkpoint(self, state: PlainLoopCursor, label: str) -> None:
        self.checkpoints.append((label, state.model_copy(deep=True)))

    @contextmanager
    def progress(self, iteration: int, total: int) -> Iterator[None]:
        self.calls.append(f"round:{iteration}/{total}")
        yield

    def log(self, message: str) -> None:
        self.calls.append(message)

    def get_issue(self, issue_id: int) -> Issue | None:
        return self.issues.get(issue_id)

    def next_open_issue(self) -> Issue | None:
        return next(
            (issue for issue in self.issues.values() if issue.status == IssueStatus.OPEN),
            None,
        )

    def list_issues(self, status: IssueStatus | None = None) -> list[Issue]:
        return [issue for issue in self.issues.values() if status is None or issue.status == status]

    def reopen_blocked(self, iteration: int) -> list[int]:
        del iteration
        reopened = [
            issue.id for issue in self.issues.values() if issue.status == IssueStatus.BLOCKED
        ]
        for issue_id in reopened:
            self.issues[issue_id] = self.issues[issue_id].model_copy(
                update={"status": IssueStatus.OPEN, "attempts": 0}
            )
        return reopened

    def claim(self, issue: Issue, iteration: int) -> Issue:
        del iteration
        claimed = issue.model_copy(update={"status": IssueStatus.IN_PROGRESS})
        self.issues[issue.id] = claimed
        self.calls.append(f"claim:{issue.id}")
        return claimed

    def block(self, issue: Issue, iteration: int, max_attempts: int) -> None:
        del iteration, max_attempts
        self.issues[issue.id] = issue.model_copy(update={"status": IssueStatus.BLOCKED})
        self.calls.append(f"block:{issue.id}")

    def increment_attempts(
        self, issue: Issue, response: IssueImplementerResponse, iteration: int
    ) -> Issue:
        del response, iteration
        attempted = issue.model_copy(update={"attempts": issue.attempts + 1})
        self.issues[issue.id] = attempted
        self.calls.append(f"attempt:{issue.id}")
        return attempted

    def close_issue(self, issue: Issue, response: IssueJudgeResponse, iteration: int) -> None:
        del response, iteration
        self.issues[issue.id] = issue.model_copy(update={"status": IssueStatus.CLOSED})
        self.calls.append(f"close:{issue.id}")

    def reopen_issue(self, issue: Issue, response: IssueJudgeResponse, iteration: int) -> None:
        del response, iteration
        self.issues[issue.id] = issue.model_copy(update={"status": IssueStatus.OPEN})
        self.calls.append(f"reopen:{issue.id}")

    def implement(self, issue: Issue) -> IssueImplementerResponse:
        self.calls.append(f"implement:{issue.id}")
        return IssueImplementerResponse(
            issue_id=issue.id, summary="changed", files_touched=[], self_check="ok"
        )

    def record_implementation(
        self, issue: Issue, response: IssueImplementerResponse, iteration: int
    ) -> None:
        del response, iteration
        self.calls.append(f"snapshot:{issue.id}")

    def judge(self, issue: Issue, iteration: int) -> IssueJudgeResponse:
        del iteration
        self.calls.append(f"judge:{issue.id}")
        verdict = next(self.verdicts)
        return IssueJudgeResponse(
            issue_id=issue.id,
            analysis="checked",
            feedback="needs work" if verdict == Verdict.FAIL else "",
            verdict=verdict,
            new_issues_filed=[],
        )

    def evaluate_performance(self, iteration: int) -> IssuePerfEvalResponse:
        self.calls.append(f"perf:{iteration}")
        new_ids = next(self.perf_issues)
        for issue_id in new_ids:
            self.issues[issue_id] = _issue(issue_id)
        return IssuePerfEvalResponse(
            analysis="measured",
            metrics=PerfMetrics(load_levels=[]),
            evaluator_feedback=[],
            new_issue_ids=new_ids,
            throughput_trend=PerfTrend.MIXED,
            latency_trend=PerfTrend.MIXED,
        )


def test_policy_runs_issue_handoffs_and_stops_after_clean_perf_eval() -> None:
    port = _FakePlainPort([], [Verdict.PASS], [[]])

    assert PlainPolicy(
        port, state=PlainLoopCursor(), max_rounds=2, max_attempts_per_issue=3, resuming=False
    ).run()

    assert port.calls[:8] == [
        "bootstrap",
        "round:1/2",
        "claim:1",
        "implement:1",
        "attempt:1",
        "snapshot:1",
        "judge:1",
        "close:1",
    ]
    assert "perf:1" in port.calls
    assert port.issues[1].status == IssueStatus.CLOSED
    assert port.checkpoints[-1][1].round_idx == 1


def test_policy_retries_failed_issue_then_blocks_without_perf_eval() -> None:
    port = _FakePlainPort([], [Verdict.FAIL, Verdict.FAIL], [])

    assert not PlainPolicy(
        port, state=PlainLoopCursor(), max_rounds=1, max_attempts_per_issue=2, resuming=False
    ).run()

    assert port.calls.count("implement:1") == 2
    assert port.calls.count("judge:1") == 2
    assert port.calls[-1].startswith("[stop] all remaining issues are blocked")
    assert "perf:1" not in port.calls
    assert port.issues[1].status == IssueStatus.BLOCKED
    assert port.checkpoints[-1][1].phase == "perf_eval"


def test_policy_resumes_at_judge_without_repeating_implementation() -> None:
    port = _FakePlainPort(
        [_issue(1, status=IssueStatus.IN_PROGRESS, attempts=1)], [Verdict.PASS], [[]]
    )
    state = PlainLoopCursor(bootstrap_done=True, phase="judge", current_issue_id=1)

    assert PlainPolicy(
        port, state=state, max_rounds=1, max_attempts_per_issue=3, resuming=True
    ).run()

    assert "implement:1" not in port.calls
    assert "judge:1" in port.calls
    assert "perf:1" in port.calls


def test_policy_runs_next_round_for_perf_filed_issue() -> None:
    port = _FakePlainPort([], [Verdict.PASS, Verdict.PASS], [[2], []])

    assert PlainPolicy(
        port, state=PlainLoopCursor(), max_rounds=2, max_attempts_per_issue=3, resuming=False
    ).run()

    assert port.calls.index("perf:1") < port.calls.index("implement:2")
    assert port.calls.count("perf:2") == 1
    assert port.issues[2].status == IssueStatus.CLOSED


def test_policy_preserves_filed_issue_when_round_budget_expires() -> None:
    port = _FakePlainPort([], [Verdict.PASS], [[2]])

    assert not PlainPolicy(
        port, state=PlainLoopCursor(), max_rounds=1, max_attempts_per_issue=3, resuming=False
    ).run()

    assert port.issues[2].status == IssueStatus.OPEN
    assert "implement:2" not in port.calls
    assert port.checkpoints[-1][0] == "plain: complete round 1"
