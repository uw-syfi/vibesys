"""Public policy behavior of the issue-queue orchestration plugin."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ValidationError

from vibesys.orchestrations.issue_queue import PLUGIN, IssueQueueOptions, IssueQueueState
from vibesys.orchestrations.issue_queue.agents import (
    IMPLEMENTER_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    PERFORMANCE_SYSTEM_PROMPT,
)
from vibesys.orchestrations.issue_queue.prompts import (
    implementer_message,
    judge_message,
    performance_message,
)
from vs_issue_board.api import IssueBoard, IssueStatus, IssueType
from vs_runtime.api import RunFacts, RunStatus
from vs_runtime.api.testing import FakeRunHost

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_runtime.api import AgentRole

IMPLEMENTER, JUDGE, PERF_EVALUATOR = PLUGIN.agents


def _options(**changes: object) -> BaseModel:
    return PLUGIN.options.model_validate(
        {
            "max_rounds": 2,
            "max_attempts_per_issue": 2,
            "max_issues_per_perf_eval": 2,
            "load_levels": ({"rate": 4, "duration": 20, "max_tokens": 64},),
            **changes,
        }
    )


def _implementation(**changes: object) -> dict[str, object]:
    return {
        "issue_id": 0,
        "summary": "Implemented the server and checks.",
        "files_touched": ("server.py", "tests/test_server.py"),
        "self_check": "Focused tests passed.",
        **changes,
    }


def _review(*, passed: bool, **changes: object) -> dict[str, object]:
    return {
        "issue_id": 0,
        "analysis": "The issue is resolved." if passed else "The health route is missing.",
        "feedback": "" if passed else "Add and test the health route.",
        "verdict": "pass" if passed else "fail",
        "new_issues_filed": (),
        **changes,
    }


def _performance(**changes: object) -> dict[str, object]:
    return {
        "analysis": "The service is measurable and no follow-up is needed.",
        "metrics": {"load_levels": (), "extra": {"peak_requests_per_second": 12.5}},
        "evaluator_feedback": ("Keep the same saturation workload.",),
        "new_issue_ids": (),
        "throughput_trend": "improved",
        "latency_trend": "improved",
        **changes,
    }


class _Script:
    def __init__(self, *replies: object) -> None:
        self.replies = deque(replies)
        self.calls: list[tuple[str, tuple[str, ...], str, type[BaseModel] | None]] = []

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role.id, history, message, response))
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _run(
    path: Path,
    script: _Script,
    *,
    options: BaseModel | None = None,
    prepare: Callable[[FakeRunHost], Awaitable[None]] | None = None,
) -> tuple[RunStatus, FakeRunHost]:
    async def scenario() -> tuple[RunStatus, FakeRunHost]:
        host = FakeRunHost(
            PLUGIN,
            project_root=path,
            facts=RunFacts(
                domain_id="llm-serving",
                objective="Build a correct and fast inference service.",
                reference_location="reference/model.py",
                accuracy_command="uv run check-accuracy",
                benchmark_command="uv run benchmark",
                accuracy_configured=True,
                benchmark_configured=True,
            ),
            responder=script.respond,
        )
        if prepare is not None:
            await prepare(host)
        try:
            status = await PLUGIN.orchestrate(host, options or _options())
            return status, host
        finally:
            await host.close()

    return asyncio.run(scenario())


def test_plugin_declares_fixed_roles_and_strict_policy_options() -> None:
    assert PLUGIN.id == "plain"
    assert PLUGIN.agents == (IMPLEMENTER, JUDGE, PERF_EVALUATOR)
    assert [tool.id for tool in JUDGE.tools] == ["shell", "issue-board"]
    assert [tool.id for tool in PERF_EVALUATOR.tools] == [
        "shell",
        "issue-board",
        "profiler",
    ]
    options = _options(load_levels=[{"rate": 1, "duration": 2, "max_tokens": 3}])
    assert len(options.model_dump()["load_levels"]) == 1

    with pytest.raises(ValidationError, match="load_levels"):
        _options(load_levels=({"rate": 0, "duration": 20, "max_tokens": 64},))
    with pytest.raises(ValidationError, match="unexpected"):
        _options(unexpected=True)


def test_success_reuses_three_named_sessions_and_writes_policy_artifacts(tmp_path: Path) -> None:
    script = _Script(_implementation(), _review(passed=True), _performance())

    status, host = _run(tmp_path, script)

    assert status is RunStatus.SUCCEEDED
    assert host.control.checkpoints == 1
    assert [call[0] for call in script.calls] == ["implementer", "judge", "perf_eval"]
    assert [session.member_id for session in host.agents.sessions] == [
        "issue-queue-implementer",
        "issue-queue-judge",
        "issue-queue-perf-evaluator",
    ]
    assert all(session.closed for session in host.agents.sessions)
    state = asyncio.run(host.state.load(IssueQueueState))
    assert state is not None
    assert state.round_idx == 1
    assert state.phase == "implementer"
    assert len(state.performance) == 1
    assert (tmp_path / "issues.json").is_file()
    assert (tmp_path / ".vibesys" / "issues" / "INDEX.md").is_file()
    assert "performance" in (tmp_path / "progress.md").read_text(encoding="utf-8")
    policy = json.loads(
        (tmp_path / ".vibesys" / "issue-tool-policy.json").read_text(encoding="utf-8")
    )
    assert policy == {
        "creator": "perf_eval",
        "iteration": 1,
        "cap": 2,
        "allowed_types": ["bug", "feature", "perf"],
    }


def test_failed_review_continues_both_role_conversations(tmp_path: Path) -> None:
    script = _Script(
        _implementation(),
        _review(passed=False),
        _implementation(summary="Added the missing route."),
        _review(passed=True),
        _performance(),
    )

    status, _host = _run(tmp_path, script)

    assert status is RunStatus.SUCCEEDED
    implementer = [call for call in script.calls if call[0] == "implementer"]
    judge = [call for call in script.calls if call[0] == "judge"]
    assert [len(call[1]) for call in implementer] == [0, 1]
    assert [len(call[1]) for call in judge] == [0, 1]
    assert "Add and test the health route" in implementer[1][2]
    assert implementer[0][3] is not None
    assert judge[0][3] is not None
    assert implementer[0][3].__name__ == "IssueImplementerResponse"
    assert judge[0][3].__name__ == "IssueJudgeResponse"


def test_attempt_gate_blocks_queue_without_paying_for_performance(tmp_path: Path) -> None:
    script = _Script(_implementation(), _review(passed=False))

    status, host = _run(
        tmp_path,
        script,
        options=_options(max_attempts_per_issue=1),
    )

    assert status is RunStatus.FAILED
    assert [call[0] for call in script.calls] == ["implementer", "judge"]
    board = IssueBoard(tmp_path / "issues.json")
    assert board.list()[0].status is IssueStatus.BLOCKED
    assert host.agents.sessions[2].history == ()


def test_resume_at_judge_does_not_repeat_implementer_turn(tmp_path: Path) -> None:
    board = IssueBoard(tmp_path / "issues.json")
    issue = board.create(
        type=IssueType.FEATURE,
        title="Recover this issue",
        description="## Acceptance criteria\n- the resumed judge passes",
        created_by="loop:bootstrap",
        iteration=1,
    )
    board.update_status(
        issue.id,
        IssueStatus.IN_PROGRESS,
        actor="loop",
        iteration=1,
    )
    board.increment_attempts(issue.id, actor="implementer", iteration=1)

    async def prepare(host: FakeRunHost) -> None:
        state = IssueQueueState.model_validate(
            {
                "round_idx": 0,
                "phase": "judge",
                "current_issue_id": issue.id,
                "bootstrap_done": True,
            }
        )
        await host.state.commit(state, workspace=host.workspaces.root, label="interrupted")

    script = _Script(_review(passed=True), _performance())

    status, _host = _run(tmp_path, script, prepare=prepare)

    assert status is RunStatus.SUCCEEDED
    assert [call[0] for call in script.calls] == ["judge", "perf_eval"]


def test_completed_performance_record_is_not_repeated_on_resume(tmp_path: Path) -> None:
    board = IssueBoard(tmp_path / "issues.json")
    issue = board.create(
        type=IssueType.FEATURE,
        title="Already complete",
        description="done",
        created_by="loop:bootstrap",
        iteration=1,
    )
    board.update_status(issue.id, IssueStatus.CLOSED, actor="judge", iteration=1)

    async def prepare(host: FakeRunHost) -> None:
        state = IssueQueueState.model_validate(
            {
                "round_idx": 0,
                "phase": "perf_eval",
                "current_issue_id": None,
                "bootstrap_done": True,
                "performance": (
                    {
                        "iteration": 1,
                        "throughput_trend": "improved",
                        "latency_trend": "improved",
                        "metrics": {"load_levels": [], "extra": {}},
                    },
                ),
            }
        )
        await host.state.commit(state, workspace=host.workspaces.root, label="measured")

    script = _Script()

    status, host = _run(tmp_path, script, prepare=prepare)

    assert status is RunStatus.SUCCEEDED
    assert script.calls == []
    assert all(session.history == () for session in host.agents.sessions)


def test_paid_turn_failure_leaves_resumable_cursor_and_closes_sessions(tmp_path: Path) -> None:
    script = _Script(RuntimeError("provider unavailable"))

    async def scenario() -> FakeRunHost:
        host = FakeRunHost(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Build the candidate."),
            responder=script.respond,
        )
        try:
            with pytest.raises(RuntimeError, match="provider unavailable"):
                await PLUGIN.orchestrate(host, _options())
            return host
        finally:
            await host.close()

    host = asyncio.run(scenario())

    state = asyncio.run(host.state.load(IssueQueueState))
    assert state is not None
    assert (state.round_idx, state.phase, state.current_issue_id) == (0, "implementer", 1)
    board = IssueBoard(tmp_path / "issues.json")
    assert board.list()[0].status is IssueStatus.IN_PROGRESS
    assert all(session.closed for session in host.agents.sessions)


@pytest.mark.parametrize(
    ("creation_script", "failed_role", "opened_sessions"),
    [
        ((RuntimeError("implementer unavailable"),), "implementer", 0),
        ((None, RuntimeError("judge unavailable")), "judge", 1),
        ((None, None, RuntimeError("perf_eval unavailable")), "perf_eval", 2),
    ],
)
def test_session_construction_failure_leaves_no_policy_artifacts(
    tmp_path: Path,
    creation_script: tuple[BaseException | None, ...],
    failed_role: str,
    opened_sessions: int,
) -> None:
    async def scenario() -> tuple[FakeRunHost, RuntimeError]:
        host = FakeRunHost(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Build the candidate."),
        )
        host.agents.script_creation(*creation_script)
        try:
            with pytest.raises(RuntimeError, match=f"{failed_role} unavailable") as raised:
                await PLUGIN.orchestrate(host, _options())
            return host, raised.value
        finally:
            await host.close()

    host, error = asyncio.run(scenario())

    assert error.args == (f"{failed_role} unavailable",)
    assert len(host.agents.sessions) == opened_sessions
    assert all(session.closed for session in host.agents.sessions)
    assert list(tmp_path.iterdir()) == []
    assert host.state.commits == ()


def test_registered_system_prompts_match_reviewed_golden() -> None:
    actual = (
        f"{IMPLEMENTER_SYSTEM_PROMPT}\n\n"
        f"=== JUDGE ===\n\n{JUDGE_SYSTEM_PROMPT}\n\n"
        f"=== PERFORMANCE ===\n\n{PERFORMANCE_SYSTEM_PROMPT}"
    )

    snapshot = Path(__file__).with_name("fixtures") / "system_prompts.txt"
    assert actual == snapshot.read_text(encoding="utf-8").rstrip("\n")


def test_prompts_preserve_legacy_role_policy_and_dynamic_context(tmp_path: Path) -> None:
    board = IssueBoard(tmp_path / "issues.json")
    issue = board.create(
        type=IssueType.FEATURE,
        title="Implement streaming",
        description="## Acceptance criteria\n- stream non-empty token deltas",
        created_by="test",
        iteration=1,
    )
    facts = RunFacts(
        domain_id="llm-serving",
        objective="Maximize measured token throughput without losing correctness.",
        reference_location="reference/model.py",
        accuracy_command="uv run check-accuracy",
        benchmark_command="uv run benchmark",
        profiler_id="torch",
        environment_notes="Use CUDA with bfloat16.",
    )
    options = IssueQueueOptions.model_validate(_options())

    implementer = implementer_message(
        issue,
        facts,
        {"feedback": "Streaming chunks were empty.", "analysis": "TPOT was null."},
    )
    judge = judge_message(issue, facts)
    performance = performance_message(
        iteration=1,
        facts=facts,
        options=options,
        state=IssueQueueState(),
    )

    assert all(
        text in IMPLEMENTER_SYSTEM_PROMPT
        for text in ("/model", "ready-made model classes", "non-empty text delta", "uv")
    )
    assert "Streaming chunks were empty" in implementer
    assert "uv run check-accuracy" in implementer
    assert '"files_touched"' in implementer
    assert all(
        text in JUDGE_SYSTEM_PROMPT
        for text in ("real server smoke test", "accuracy checker", "at most one bug")
    )
    assert "positive token throughput" in judge
    assert '"verdict": "pass" | "fail"' in judge
    assert [tool.id for tool in PERF_EVALUATOR.tools] == [
        "shell",
        "issue-board",
        "profiler",
    ]
    assert all(
        text in PERFORMANCE_SYSTEM_PROMPT
        for text in (
            "identified stale server",
            "workload shape",
            "all-time best",
            "limiting resource at each load level",
            "at most one profile",
            "impact over effort",
        )
    )
    assert "Profiler `torch` is available through the fixed `profiler` tool" in performance
    assert "increase request rate until throughput plateaus" in performance
    assert "prompt-length or output-length workloads" in performance
    assert "Always\nstop any server process" in performance
    assert "code_evidence" in performance
    assert "list_issues" in performance
    assert '"throughput_trend": "improved" | "regressed" | "mixed"' in performance

    without_profiler = performance_message(
        iteration=1,
        facts=facts.model_copy(update={"profiler_id": "none"}),
        options=options,
        state=IssueQueueState(),
    )
    assert "No profiler is configured; do not attempt a profile." in without_profiler
