"""End-to-end tests for the ``plain`` (issue_queue) orchestrator.

Drives ``IssueQueueOrchestrator.run(ctx)`` through ``run_orchestration``'s
real fake seams (``agent_client_factory`` / ``backend_factory``), never
``unittest.mock.patch`` on a collaborator: see ``_support.run_plain``.

The clean-pass, retry-then-pass, and perf-eval-with-metrics scenarios (and
every board file / prompt / event they produce) are golden-snapshotted in
``tests/vibesys/golden/test_issue_queue_golden.py``; this module only covers
behavior that snapshot does not: blocking after exhausted attempts, resume
after a crash (idempotent bootstrap, blocked-issue retry, budget-increase
resume rules), per-phase issue-board tool scoping, and cursor persistence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.vibesys.loops.issue_queue._support import (
    implementer_response,
    issue_queue_options,
    judge_response,
    perf_eval_response,
    plain_local_dir,
    plain_state_store,
    run_plain,
)

from vibesys.errors import ConfigurationError
from vibesys.loops.issue_queue.orchestration import descriptor_from_options
from vibesys.roles.common import Verdict
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient
from vs_issue_tracker.api import IssueBoard, IssueStatus
from vs_project.api import OrchestrationRunManifest, Project

if TYPE_CHECKING:
    from pathlib import Path


def _client() -> FakeAgentClient:
    return FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(tool_servers=True))


def _board(project_dir: Path) -> IssueBoard:
    return IssueBoard(project_dir / "issues.json")


def test_bootstrap_is_not_repeated_on_resume(tmp_path: Path) -> None:
    """A resumed run with ``bootstrap_done=True`` must not re-file the issue."""
    first = _client()
    first.enqueue("implementer", implementer_response(1))
    first.enqueue("judge", judge_response(1, Verdict.PASS))
    first.enqueue("perf_eval", perf_eval_response())
    run1 = run_plain(tmp_path, first, exp_name="resume-bootstrap")
    assert run1.result is True
    assert len(_board(run1.project_dir).list()) == 1

    second = _client()
    second.enqueue("perf_eval", perf_eval_response())
    run2 = run_plain(
        tmp_path,
        second,
        descriptor=descriptor_from_options(issue_queue_options(max_rounds=2)),
        resume_from=run1,
    )

    assert run2.result is True
    issues = _board(run2.project_dir).list()
    assert len(issues) == 1
    assert issues[0].created_by == "loop:bootstrap"
    assert [call.kind for call in second.calls] == ["perf_eval"]


def test_bootstrap_issue_uses_the_run_objective(tmp_path: Path) -> None:
    """The initial issue describes this task instead of assuming model serving."""
    fake = _client()
    fake.enqueue("implementer", implementer_response(1))
    fake.enqueue("judge", judge_response(1, Verdict.PASS))
    fake.enqueue("perf_eval", perf_eval_response())
    run = run_plain(
        tmp_path,
        fake,
        exp_name="objective-bootstrap",
    )

    issue = _board(run.project_dir).get(1)
    assert issue is not None
    assert issue.title == "Initial task: Maximize tok/s throughput."
    assert "FastAPI" not in issue.description
    assert "Maximize tok/s throughput." in issue.description
    for kind in ("implementer", "judge", "perf_eval"):
        prompt = fake.calls_for(kind)[0].system_prompt
        assert "FastAPI inference server" not in prompt
        assert "VibeServeModel" not in prompt


def test_issue_blocks_after_max_attempts_exhausted(tmp_path: Path) -> None:
    """Repeated FAIL verdicts block the issue instead of retrying forever."""
    fake = _client()
    fake.enqueue("implementer", implementer_response(1), implementer_response(1))
    fake.enqueue(
        "judge",
        judge_response(1, Verdict.FAIL, feedback="Still broken."),
        judge_response(1, Verdict.FAIL, feedback="Still broken."),
    )
    run = run_plain(
        tmp_path,
        fake,
        descriptor=descriptor_from_options(issue_queue_options(max_attempts_per_issue=2)),
        exp_name="blocks",
    )

    assert run.result is False
    issue = _board(run.project_dir).get(1)
    assert issue is not None
    assert issue.status == IssueStatus.BLOCKED
    assert issue.attempts == 2


def test_resume_retries_previously_blocked_issue_with_reset_attempts(tmp_path: Path) -> None:
    """A blocked issue is reopened with a fresh attempt budget on resume."""
    first = _client()
    first.enqueue("implementer", implementer_response(1), implementer_response(1))
    first.enqueue(
        "judge",
        judge_response(1, Verdict.FAIL, feedback="nope"),
        judge_response(1, Verdict.FAIL, feedback="still nope"),
    )
    descriptor = descriptor_from_options(issue_queue_options(max_attempts_per_issue=2))
    run1 = run_plain(tmp_path, first, descriptor=descriptor, exp_name="resume-blocked")
    assert run1.result is False
    blocked = _board(run1.project_dir).get(1)
    assert blocked is not None
    assert blocked.status == IssueStatus.BLOCKED

    second = _client()
    second.enqueue("implementer", implementer_response(1, summary="Fixed."))
    second.enqueue("judge", judge_response(1, Verdict.PASS))
    second.enqueue("perf_eval", perf_eval_response())
    run2 = run_plain(tmp_path, second, descriptor=descriptor, resume_from=run1)

    assert run2.result is True
    resumed = _board(run2.project_dir).get(1)
    assert resumed is not None
    assert resumed.status == IssueStatus.CLOSED
    # Reopen resets the attempt counter; the successful retry counts as attempt 1.
    assert resumed.attempts == 1
    assert "blocked->open" in [evt.action for evt in resumed.history]


def test_budget_increase_on_resume_requires_clean_workspace(tmp_path: Path) -> None:
    first = _client()
    first.enqueue("implementer", implementer_response(1))
    first.enqueue("judge", judge_response(1, Verdict.PASS))
    first.enqueue("perf_eval", perf_eval_response())
    run1 = run_plain(tmp_path, first, exp_name="budget-increase")

    pending = run1.project_dir / "pending-change.txt"
    pending.write_text("uncommitted candidate change")
    bumped = descriptor_from_options(issue_queue_options(max_rounds=2))

    with pytest.raises(ConfigurationError, match="commit or discard pending project changes"):
        run_plain(tmp_path, _client(), descriptor=bumped, resume_from=run1)
    recorded = Project.open(run1.project_dir).state.load_run(run1.run_id)
    assert isinstance(recorded, OrchestrationRunManifest)
    assert recorded.orchestration.options["max_rounds"] == 1

    pending.unlink()
    resumed = _client()
    resumed.enqueue("perf_eval", perf_eval_response())
    resumed.enqueue("perf_eval", perf_eval_response())
    run2 = run_plain(tmp_path, resumed, descriptor=bumped, resume_from=run1)
    updated = Project.open(run2.project_dir).state.load_run(run2.run_id)
    assert isinstance(updated, OrchestrationRunManifest)
    assert updated.orchestration.options["max_rounds"] == 2


def test_phase_ordering_and_issue_board_tool_scoping(tmp_path: Path) -> None:
    """impl -> judge -> perf_eval, with per-phase issue-board tool access.

    Only judge and perf_eval get the issue-board MCP tool (scoped by
    creator/cap/allowed-types); the implementer works from the issue
    inlined in its prompt and gets no tool access.
    """
    fake = _client()
    fake.enqueue("implementer", implementer_response(1))
    fake.enqueue("judge", judge_response(1, Verdict.PASS))
    fake.enqueue("perf_eval", perf_eval_response())
    run_plain(
        tmp_path,
        fake,
        descriptor=descriptor_from_options(issue_queue_options(max_issues_per_perf_eval=2)),
        exp_name="ordering",
    )

    assert [call.kind for call in fake.calls] == ["implementer", "judge", "perf_eval"]

    impl_call = fake.calls_for("implementer")[0]
    assert not impl_call.mcp_servers

    judge_call = fake.calls_for("judge")[0]
    assert judge_call.mcp_servers
    judge_spec = judge_call.mcp_servers[0]
    assert judge_spec.name == "vibesys-issues"
    judge_args = _flags(judge_spec.args)
    assert judge_args == {
        "creator": "judge",
        "iteration": "1",
        "cap": "1",
        "allowed-types": "bug",
    }

    perf_call = fake.calls_for("perf_eval")[0]
    assert "Use only the tools exposed by the `vibesys-issues` MCP server" in (
        perf_call.system_prompt
    )
    assert "bypass the run's selected tracker backend" in perf_call.system_prompt
    assert perf_call.mcp_servers
    perf_spec = perf_call.mcp_servers[0]
    perf_args = _flags(perf_spec.args)
    assert perf_args == {
        "creator": "perf_eval",
        "iteration": "1",
        "cap": "2",
        "allowed-types": "bug,feature,perf",
    }


def _flags(args: tuple[str, ...]) -> dict[str, str]:
    out: dict[str, str] = {}
    it = iter(args)
    for token in it:
        if token.startswith("--"):
            out[token.lstrip("-")] = next(it)
    return out


def test_state_json_reflects_bootstrap_and_performance_after_run(tmp_path: Path) -> None:
    fake = _client()
    fake.enqueue("implementer", implementer_response(1))
    fake.enqueue("judge", judge_response(1, Verdict.PASS))
    fake.enqueue("perf_eval", perf_eval_response())
    run = run_plain(tmp_path, fake, exp_name="state-json")

    store = plain_state_store(run.project_dir, run.run_id)
    cursor = store.load_cursor()
    assert cursor is not None
    assert cursor.bootstrap_done is True
    assert cursor.round_idx >= 0
    assert store.load_performance().records
    assert not (run.project_dir / "logs").exists()
    assert not (plain_local_dir(run.project_dir, run.run_id) / "issues.json").exists()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
