"""``IssueQueueOrchestrator``'s drain/round control flow against a real
``IssueBoard``, driven through the real orchestrator (``_support.run_plain``,
which uses ``run_orchestration``'s ``agent_client_factory``/``backend_factory``
seams with ``FakeAgentClient``/``FakeComputeBackend``, never
``unittest.mock.patch`` on a collaborator).

The clean pass / retry-then-pass / perf-eval-with-metrics scenarios (and
every board file / prompt / event they produce) are golden-snapshotted in
``tests/vibesys/golden/test_issue_queue_golden.py``; blocking after
exhausted attempts and resume-after-crash mechanics are covered in
``test_plain_loop.py``. This module covers the control-flow edges those
suites don't: stopping before the round budget is exhausted once the board
drains cleanly, resuming mid-judge without repeating the implementer,
issues perf_eval files getting processed in the next round, and a
round-budget expiry preserving a still-open perf-eval-filed issue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.vibesys.loops.issue_queue._support import (
    implementer_response,
    issue_queue_options,
    judge_response,
    perf_eval_files_issue_callback,
    perf_eval_response,
    plain_state_store,
    run_plain,
    run_plain_expect_crash,
)

from vibesys.loops.issue_queue.orchestration import descriptor_from_options
from vibesys.roles.common import Verdict
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_issue_board.api import IssueBoard, IssueStatus

if TYPE_CHECKING:
    from pathlib import Path


class _JudgeCrashError(Exception):
    """Stand-in for a killed process, raised mid-judge-turn."""


def _client() -> FakeAgentClient:
    return FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))


def _board(project_dir: Path) -> IssueBoard:
    return IssueBoard(project_dir / "issues.json")


def test_stops_after_clean_perf_eval_before_round_budget_exhausted(tmp_path: Path) -> None:
    """A clean drain returns early, without spending the full round budget."""
    fake = _client()
    fake.enqueue("implementer", implementer_response(1))
    fake.enqueue("judge", judge_response(1, Verdict.PASS))
    fake.enqueue("perf_eval", perf_eval_response())
    run = run_plain(
        tmp_path,
        fake,
        descriptor=descriptor_from_options(issue_queue_options(max_rounds=2)),
        exp_name="stops-early",
    )

    assert run.result is True
    assert [call.kind for call in fake.calls] == ["implementer", "judge", "perf_eval"]
    issue = _board(run.project_dir).get(1)
    assert issue is not None
    assert issue.status == IssueStatus.CLOSED
    cursor = plain_state_store(run.project_dir, run.run_id).load_cursor()
    assert cursor is not None
    assert cursor.round_idx == 1  # stopped after round 1 of a 2-round budget


def test_blocked_board_skips_performance_evaluation(tmp_path: Path) -> None:
    """When every remaining issue is blocked, perf_eval is never invoked."""
    fake = _client()
    fake.enqueue("implementer", implementer_response(1), implementer_response(1))
    fake.enqueue(
        "judge",
        judge_response(1, Verdict.FAIL, feedback="nope"),
        judge_response(1, Verdict.FAIL, feedback="still nope"),
    )
    run = run_plain(
        tmp_path,
        fake,
        descriptor=descriptor_from_options(issue_queue_options(max_attempts_per_issue=2)),
        exp_name="blocked-skips-perf",
    )

    assert run.result is False
    assert fake.calls_for("perf_eval") == []
    issue = _board(run.project_dir).get(1)
    assert issue is not None
    assert issue.status == IssueStatus.BLOCKED


def test_resumes_at_judge_without_repeating_implementation(tmp_path: Path) -> None:
    """A crash after the implementer, mid-judge-turn, resumes at judge only.

    Injected through ``FakeAgentClient.fail`` (a real client seam), not
    ``unittest.mock.patch``: the judge-phase checkpoint commits before the
    judge turn runs, so a crash there leaves the same on-disk state a real
    kill mid-judge would.
    """
    crasher = _client()
    crasher.enqueue("implementer", implementer_response(1))
    crasher.fail("judge", _JudgeCrashError())
    crashed = run_plain_expect_crash(
        tmp_path, crasher, _JudgeCrashError, exp_name="resume-at-judge"
    )
    issue = _board(crashed.project_dir).get(1)
    assert issue is not None
    assert issue.status == IssueStatus.IN_PROGRESS
    assert issue.attempts == 1

    resumer = _client()
    resumer.enqueue("judge", judge_response(1, Verdict.PASS))
    resumer.enqueue("perf_eval", perf_eval_response())
    resumed = run_plain(tmp_path, resumer, resume_from=crashed)

    assert resumed.result is True
    assert resumer.calls_for("implementer") == []
    assert len(resumer.calls_for("judge")) == 1
    assert len(resumer.calls_for("perf_eval")) == 1
    issue = _board(resumed.project_dir).get(1)
    assert issue is not None
    assert issue.status == IssueStatus.CLOSED
    assert issue.attempts == 1  # the crashed attempt was not redone


def test_perf_eval_filed_issue_is_processed_in_next_round(tmp_path: Path) -> None:
    fake = _client()
    fake.on_invoke(perf_eval_files_issue_callback(fake))
    fake.enqueue("implementer", implementer_response(1), implementer_response(2))
    fake.enqueue("judge", judge_response(1, Verdict.PASS), judge_response(2, Verdict.PASS))
    fake.enqueue("perf_eval", perf_eval_response(), perf_eval_response())
    run = run_plain(
        tmp_path,
        fake,
        descriptor=descriptor_from_options(issue_queue_options(max_rounds=2)),
        exp_name="perf-files-issue",
    )

    assert run.result is True
    assert [call.kind for call in fake.calls] == [
        "implementer",
        "judge",
        "perf_eval",
        "implementer",
        "judge",
        "perf_eval",
    ]
    issue = _board(run.project_dir).get(2)
    assert issue is not None
    assert issue.status == IssueStatus.CLOSED


def test_perf_eval_filed_issue_preserved_when_round_budget_expires(tmp_path: Path) -> None:
    fake = _client()
    fake.on_invoke(perf_eval_files_issue_callback(fake))
    fake.enqueue("implementer", implementer_response(1))
    fake.enqueue("judge", judge_response(1, Verdict.PASS))
    fake.enqueue("perf_eval", perf_eval_response())
    run = run_plain(tmp_path, fake, exp_name="perf-files-issue-budget")  # max_rounds=1 by default

    assert run.result is False
    assert fake.calls_for("implementer") == fake.calls_for("implementer")[:1]
    issue = _board(run.project_dir).get(2)
    assert issue is not None
    assert issue.status == IssueStatus.OPEN
    cursor = plain_state_store(run.project_dir, run.run_id).load_cursor()
    assert cursor is not None
    assert cursor.round_idx == 1
    assert cursor.phase == "implementer"
