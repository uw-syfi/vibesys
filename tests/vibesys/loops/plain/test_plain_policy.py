"""Plain orchestration decisions against a real issue board and fake turns."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, cast
from unittest.mock import AsyncMock, patch

from vibesys.loops.issue_queue.entrypoint import IssueQueueOrchestrator
from vibesys.loops.issue_queue.orchestration import IssueQueueOptions, descriptor_from_options
from vibesys.schemas import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    PerfMetrics,
    PerfTrend,
    Verdict,
)
from vs_issue_board.api import IssueBoard, IssueStatus, IssueType
from vs_loop_state.api import PlainLoopCursor

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext
    from vs_issue_board.api import Issue

PlainPhase = Literal["implementer", "judge", "perf_eval"]


class _Context:
    def __init__(self) -> None:
        self.control = self

    async def boundary(self) -> None:
        return


class _PlainRun:
    def __init__(
        self,
        path: Path,
        verdicts: list[Verdict],
        perf_issues: list[list[int]],
        *,
        state: PlainLoopCursor | None = None,
        resuming: bool = False,
    ) -> None:
        self.host = _Context()
        self.board = IssueBoard(path / "issues.json")
        self.turns = self
        self.state = state or PlainLoopCursor()
        self.resuming = resuming
        self.verdicts = iter(verdicts)
        self.perf_issues = iter(perf_issues)
        self.calls: list[str] = []
        self.checkpoints: list[tuple[str, PlainLoopCursor]] = []

    async def bootstrap(self) -> None:
        if self.state.bootstrap_done:
            return
        self.board.create(
            type=IssueType.FEATURE,
            title="Initial issue",
            description="Improve the candidate",
            created_by="loop:bootstrap",
            iteration=1,
        )
        self.state = self.state.model_copy(update={"bootstrap_done": True})
        self.calls.append("bootstrap")
        await self.checkpoint(0, "implementer", None, "plain: initialize issue board")

    async def prepare_resume(self) -> None:
        if self.resuming:
            self.board.reopen_blocked(actor="loop:resume", iteration=self.state.round_idx + 1)

    async def checkpoint(
        self, round_idx: int, phase: PlainPhase, issue_id: int | None, label: str
    ) -> None:
        self.state = self.state.transition(
            round_idx=round_idx, phase=phase, current_issue_id=issue_id
        )
        self.checkpoints.append((label, self.state.model_copy(deep=True)))

    def performance_record(self, iteration: int) -> None:
        del iteration

    @contextmanager
    def progress(self, iteration: int, total: int) -> Iterator[None]:
        self.calls.append(f"round:{iteration}/{total}")
        yield

    def log(self, message: str) -> None:
        self.calls.append(message)

    async def implement(self, issue: Issue) -> IssueImplementerResponse:
        self.calls.append(f"implement:{issue.id}")
        return IssueImplementerResponse(
            issue_id=issue.id, summary="changed", files_touched=[], self_check="ok"
        )

    async def record_implementation(
        self, issue: Issue, response: IssueImplementerResponse, iteration: int
    ) -> None:
        del response, iteration
        self.calls.append(f"snapshot:{issue.id}")

    async def judge(self, issue: Issue, iteration: int) -> IssueJudgeResponse:
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

    async def evaluate_performance(
        self, iteration: int, cursor: PlainLoopCursor
    ) -> IssuePerfEvalResponse:
        del cursor
        self.calls.append(f"perf:{iteration}")
        new_ids = next(self.perf_issues)
        for issue_id in new_ids:
            issue = self.board.create(
                type=IssueType.BUG,
                title=f"Issue {issue_id}",
                description="Improve the candidate",
                created_by="perf_eval",
                iteration=iteration,
            )
            assert issue.id == issue_id
        return IssuePerfEvalResponse(
            analysis="measured",
            metrics=PerfMetrics(load_levels=[]),
            evaluator_feedback=[],
            new_issue_ids=new_ids,
            throughput_trend=PerfTrend.MIXED,
            latency_trend=PerfTrend.MIXED,
        )


def _run_policy(run: _PlainRun, *, max_rounds: int, max_attempts: int = 3) -> bool:
    options = IssueQueueOptions(
        max_rounds=max_rounds,
        max_attempts_per_issue=max_attempts,
        max_issues_per_perf_eval=3,
    )
    policy = IssueQueueOrchestrator(descriptor_from_options(options))
    with patch(
        "vibesys.loops.issue_queue.entrypoint.IssueQueueRun.open",
        new=AsyncMock(return_value=run),
    ):
        return asyncio.run(policy.run(cast("RunContext", run.host)))


def test_policy_runs_issue_handoffs_and_stops_after_clean_perf_eval(tmp_path: Path) -> None:
    run = _PlainRun(tmp_path, [Verdict.PASS], [[]])
    assert _run_policy(run, max_rounds=2)
    assert run.calls[:4] == ["bootstrap", "round:1/2", "implement:1", "snapshot:1"]
    assert "judge:1" in run.calls
    assert "perf:1" in run.calls
    issue = run.board.get(1)
    assert issue is not None
    assert issue.status == IssueStatus.CLOSED
    assert run.checkpoints[-1][1].round_idx == 1


def test_policy_retries_failed_issue_then_blocks_without_perf_eval(tmp_path: Path) -> None:
    run = _PlainRun(tmp_path, [Verdict.FAIL, Verdict.FAIL], [])
    assert not _run_policy(run, max_rounds=1, max_attempts=2)
    assert run.calls.count("implement:1") == 2
    assert run.calls.count("judge:1") == 2
    assert "perf:1" not in run.calls
    issue = run.board.get(1)
    assert issue is not None
    assert issue.status == IssueStatus.BLOCKED
    assert run.checkpoints[-1][1].phase == "perf_eval"


def test_policy_resumes_at_judge_without_repeating_implementation(tmp_path: Path) -> None:
    state = PlainLoopCursor(bootstrap_done=True, phase="judge", current_issue_id=1)
    run = _PlainRun(tmp_path, [Verdict.PASS], [[]], state=state, resuming=True)
    issue = run.board.create(
        type=IssueType.FEATURE,
        title="Issue 1",
        description="Improve the candidate",
        created_by="test",
        iteration=1,
    )
    run.board.update_status(issue.id, IssueStatus.IN_PROGRESS, actor="loop", iteration=1)
    run.board.increment_attempts(issue.id, actor="implementer", iteration=1)
    assert _run_policy(run, max_rounds=1)
    assert "implement:1" not in run.calls
    assert "judge:1" in run.calls
    assert "perf:1" in run.calls


def test_policy_runs_next_round_for_perf_filed_issue(tmp_path: Path) -> None:
    run = _PlainRun(tmp_path, [Verdict.PASS, Verdict.PASS], [[2], []])
    assert _run_policy(run, max_rounds=2)
    assert run.calls.index("perf:1") < run.calls.index("implement:2")
    assert run.calls.count("perf:2") == 1
    issue = run.board.get(2)
    assert issue is not None
    assert issue.status == IssueStatus.CLOSED


def test_policy_preserves_filed_issue_when_round_budget_expires(tmp_path: Path) -> None:
    run = _PlainRun(tmp_path, [Verdict.PASS], [[2]])
    assert not _run_policy(run, max_rounds=1)
    issue = run.board.get(2)
    assert issue is not None
    assert issue.status == IssueStatus.OPEN
    assert "implement:2" not in run.calls
    assert run.checkpoints[-1][0] == "plain: complete round 1"
