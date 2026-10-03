"""Public policy behavior of the issue-queue orchestration plugin."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ValidationError

from vibesys.orchestration.issue_queue import PLUGIN, IssueQueueOptions, IssueQueueState
from vibesys.orchestration.issue_queue.agents import (
    IMPLEMENTER_SYSTEM_PROMPT,
    JUDGE_SYSTEM_PROMPT,
    PERFORMANCE_SYSTEM_PROMPT,
)
from vibesys.orchestration.issue_queue.prompts import (
    implementer_message,
    judge_message,
    performance_message,
)
from vs_issue_tracker.api import IssueBoard, IssueStatus, IssueTrackerConfig, IssueType
from vs_runtime.api import AgentCapability, RunFacts, RunStatus
from vs_runtime.api.testing import FakeRun

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from vs_runtime.api import AgentRole

IMPLEMENTER, JUDGE, PERF_EVALUATOR = PLUGIN.agents
_FAKE_AGENT_CAPABILITIES = frozenset(
    {
        AgentCapability.MCP_SERVERS,
        AgentCapability.PROVIDER_SESSION_RESUME,
        AgentCapability.SESSION_REUSE,
    }
)
_FAKE_AGENT_TOOLS = frozenset({"issue-board", "profiler"})


def _options(**changes: object) -> IssueQueueOptions:
    return IssueQueueOptions.model_validate(
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
    def __init__(
        self,
        *replies: object,
        effect: Callable[[AgentRole, int], None] | None = None,
    ) -> None:
        self.replies = deque(replies)
        self.calls: list[tuple[str, tuple[str, ...], str, type[BaseModel] | None]] = []
        self.effect = effect

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role.id, history, message, response))
        if self.effect is not None:
            self.effect(role, len(self.calls))
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _run(
    path: Path,
    script: _Script,
    *,
    options: BaseModel | None = None,
    prepare: Callable[[FakeRun], Awaitable[None]] | None = None,
) -> tuple[RunStatus, FakeRun]:
    async def scenario() -> tuple[RunStatus, FakeRun]:
        run = _fake_host(path, script)
        if prepare is not None:
            await prepare(run)
        try:
            status = await PLUGIN.orchestrate(run, options or _options())
            return status, run
        finally:
            await run.close()

    return asyncio.run(scenario())


def _fake_host(path: Path, script: _Script) -> FakeRun:
    return FakeRun(
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
        supported_extra_tools=_FAKE_AGENT_TOOLS,
        supported_agent_capabilities=_FAKE_AGENT_CAPABILITIES,
    )


def test_plugin_declares_fixed_roles_and_strict_policy_options() -> None:
    assert PLUGIN.id == "plain"
    assert PLUGIN.agents == (IMPLEMENTER, JUDGE, PERF_EVALUATOR)
    assert [tool.id for tool in JUDGE.extra_tools] == ["issue-board"]
    assert [tool.id for tool in PERF_EVALUATOR.extra_tools] == [
        "issue-board",
        "profiler",
    ]
    options = _options(load_levels=[{"rate": 1, "duration": 2, "max_tokens": 3}])
    assert len(options.model_dump()["load_levels"]) == 1

    with pytest.raises(ValidationError, match="load_levels"):
        _options(load_levels=({"rate": 0, "duration": 20, "max_tokens": 64},))
    with pytest.raises(ValidationError, match="unexpected"):
        _options(unexpected=True)


def test_tracker_selection_is_strict_plugin_configuration() -> None:
    options = _options(
        tracker=IssueTrackerConfig.from_backend("github", repository="owner/repository")
    )

    assert options.tracker.backend == "github"
    assert options.tracker.repository == "owner/repository"
    with pytest.raises(ValidationError, match="repository is required"):
        _options(tracker={"backend": "github"})
    with pytest.raises(ValidationError, match="OWNER/REPOSITORY"):
        _options(tracker={"backend": "github", "repository": "repository-only"})
    with pytest.raises(ValidationError, match="unexpected"):
        _options(tracker={"backend": "local", "unexpected": True})


def test_success_reuses_three_named_sessions_and_writes_policy_artifacts(tmp_path: Path) -> None:
    script = _Script(_implementation(), _review(passed=True), _performance())

    status, run = _run(tmp_path, script)

    assert status is RunStatus.SUCCEEDED
    assert run.control.checkpoints == 1
    assert [call[0] for call in script.calls] == ["implementer", "judge", "perf_eval"]
    assert [session.member_id for session in run.agents.sessions] == [
        "issue-queue-implementer",
        "issue-queue-judge",
        "issue-queue-perf-evaluator",
    ]
    assert all(session.closed for session in run.agents.sessions)
    state = asyncio.run(run.state.load(IssueQueueState))
    assert state is not None
    assert state.round_idx == 1
    assert state.phase == "implementer"
    assert len(state.performance) == 1
    assert (tmp_path / "issues.json").is_file()
    tracker_config = json.loads(
        (tmp_path / ".vibesys" / "issue-tracker.json").read_text(encoding="utf-8")
    )
    assert tracker_config == {"backend": "local", "repository": None}
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

    status, run = _run(
        tmp_path,
        script,
        options=_options(max_attempts_per_issue=1),
    )

    assert status is RunStatus.FAILED
    assert [call[0] for call in script.calls] == ["implementer", "judge"]
    board = IssueBoard(tmp_path / "issues.json")
    assert board.list()[0].status is IssueStatus.BLOCKED
    assert run.agents.sessions[2].history == ()


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

    async def prepare(run: FakeRun) -> None:
        state = IssueQueueState.model_validate(
            {
                "round_idx": 0,
                "phase": "judge",
                "current_issue_id": issue.id,
                "bootstrap_done": True,
            }
        )
        await run.state.commit(state, workspace=run.workspaces.root, label="interrupted")

    script = _Script(_review(passed=True), _performance())

    status, _host = _run(tmp_path, script, prepare=prepare)

    assert status is RunStatus.SUCCEEDED
    assert [call[0] for call in script.calls] == ["judge", "perf_eval"]


def test_resume_ignores_stale_closed_issue_and_drains_open_work(tmp_path: Path) -> None:
    board = IssueBoard(tmp_path / "issues.json")
    stale = board.create(
        type=IssueType.BUG,
        title="Already closed",
        description="done",
        created_by="judge",
        iteration=1,
    )
    board.update_status(stale.id, IssueStatus.CLOSED, actor="judge", iteration=1)
    active = board.create(
        type=IssueType.BUG,
        title="Still open",
        description="fix this",
        created_by="judge",
        iteration=1,
    )

    async def prepare(run: FakeRun) -> None:
        await run.state.commit(
            IssueQueueState(
                round_idx=0,
                phase="judge",
                current_issue_id=stale.id,
                bootstrap_done=True,
            ),
            workspace=run.workspaces.root,
            label="stale cursor",
        )

    script = _Script(_implementation(), _review(passed=True), _performance())

    status, _host = _run(tmp_path, script, prepare=prepare)

    assert status is RunStatus.SUCCEEDED
    assert [call[0] for call in script.calls] == ["implementer", "judge", "perf_eval"]
    reloaded = IssueBoard(tmp_path / "issues.json")
    stale_after = reloaded.get(stale.id)
    active_after = reloaded.get(active.id)
    assert stale_after is not None
    assert active_after is not None
    assert stale_after.attempts == 0
    assert active_after.status is IssueStatus.CLOSED


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

    async def prepare(run: FakeRun) -> None:
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
        await run.state.commit(state, workspace=run.workspaces.root, label="measured")

    script = _Script()

    status, run = _run(tmp_path, script, prepare=prepare)

    assert status is RunStatus.SUCCEEDED
    assert script.calls == []
    assert all(session.history == () for session in run.agents.sessions)


def test_bootstrap_is_idempotent_across_repeated_plugin_invocation(tmp_path: Path) -> None:
    script = _Script(
        _implementation(),
        _review(passed=True),
        _performance(),
        _performance(),
    )

    async def scenario() -> tuple[RunStatus, RunStatus, FakeRun]:
        run = _fake_host(tmp_path, script)
        try:
            first = await PLUGIN.orchestrate(run, _options(max_rounds=1))
            second = await PLUGIN.orchestrate(run, _options(max_rounds=2))
            return first, second, run
        finally:
            await run.close()

    first, second, run = asyncio.run(scenario())

    assert (first, second) == (RunStatus.SUCCEEDED, RunStatus.SUCCEEDED)
    assert [call[0] for call in script.calls] == [
        "implementer",
        "judge",
        "perf_eval",
        "perf_eval",
    ]
    issues = IssueBoard(tmp_path / "issues.json").list()
    assert len(issues) == 1
    assert issues[0].created_by == "loop:bootstrap"
    assert len([event for event in issues[0].history if event.action == "create"]) == 1
    state = asyncio.run(run.state.load(IssueQueueState))
    assert state is not None
    assert [record.iteration for record in state.performance] == [1, 2]


def test_blocked_issue_reopens_after_total_round_budget_increases(tmp_path: Path) -> None:
    board = IssueBoard(tmp_path / "issues.json")
    issue = board.create(
        type=IssueType.BUG,
        title="Retry after more budget",
        description="Fix the candidate.",
        created_by="perf_eval",
        iteration=1,
    )
    board.update_status(
        issue.id,
        IssueStatus.BLOCKED,
        actor="loop",
        iteration=1,
        note="budget exhausted",
    )
    script = _Script(_implementation(), _review(passed=True), _performance())

    async def scenario() -> tuple[RunStatus, RunStatus, FakeRun]:
        run = _fake_host(tmp_path, script)
        await run.state.commit(
            IssueQueueState(
                round_idx=1,
                phase="implementer",
                bootstrap_done=True,
            ),
            workspace=run.workspaces.root,
            label="budget exhausted",
        )
        try:
            unchanged = await PLUGIN.orchestrate(run, _options(max_rounds=1))
            increased = await PLUGIN.orchestrate(run, _options(max_rounds=2))
            return unchanged, increased, run
        finally:
            await run.close()

    unchanged, increased, _host = asyncio.run(scenario())

    assert unchanged is RunStatus.FAILED
    assert increased is RunStatus.SUCCEEDED
    resumed = IssueBoard(tmp_path / "issues.json").get(issue.id)
    assert resumed is not None
    assert resumed.status is IssueStatus.CLOSED
    assert resumed.attempts == 1
    assert "blocked->open" in [event.action for event in resumed.history]
    assert [call[0] for call in script.calls] == ["implementer", "judge", "perf_eval"]


def test_performance_follow_on_issue_is_processed_in_next_round(tmp_path: Path) -> None:
    perf_turns = 0

    def file_follow_on(role: AgentRole, _call_number: int) -> None:
        nonlocal perf_turns
        if role.id != "perf_eval":
            return
        perf_turns += 1
        if perf_turns == 1:
            IssueBoard(tmp_path / "issues.json").create(
                type=IssueType.PERF,
                title="Batch decode requests",
                description="Reduce launch overhead.",
                created_by="perf_eval",
                iteration=1,
            )

    script = _Script(
        _implementation(),
        _review(passed=True),
        _performance(new_issue_ids=(2,)),
        _implementation(issue_id=2),
        _review(passed=True, issue_id=2),
        _performance(),
        effect=file_follow_on,
    )

    status, _host = _run(tmp_path, script, options=_options(max_rounds=2))

    assert status is RunStatus.SUCCEEDED
    assert [call[0] for call in script.calls] == [
        "implementer",
        "judge",
        "perf_eval",
        "implementer",
        "judge",
        "perf_eval",
    ]
    follow_on = IssueBoard(tmp_path / "issues.json").get(2)
    assert follow_on is not None
    assert follow_on.status is IssueStatus.CLOSED
    assert follow_on.attempts == 1


def test_performance_follow_on_issue_survives_round_budget_expiry(tmp_path: Path) -> None:
    def file_follow_on(role: AgentRole, _call_number: int) -> None:
        if role.id == "perf_eval":
            IssueBoard(tmp_path / "issues.json").create(
                type=IssueType.PERF,
                title="Batch decode requests",
                description="Reduce launch overhead.",
                created_by="perf_eval",
                iteration=1,
            )

    script = _Script(
        _implementation(),
        _review(passed=True),
        _performance(new_issue_ids=(2,)),
        effect=file_follow_on,
    )

    status, run = _run(tmp_path, script, options=_options(max_rounds=1))

    assert status is RunStatus.FAILED
    follow_on = IssueBoard(tmp_path / "issues.json").get(2)
    assert follow_on is not None
    assert follow_on.status is IssueStatus.OPEN
    state = asyncio.run(run.state.load(IssueQueueState))
    assert state is not None
    assert (state.round_idx, state.phase) == (1, "implementer")


def test_retry_trajectory_matches_golden_policy_snapshot(tmp_path: Path) -> None:
    script = _Script(
        _implementation(summary="First attempt."),
        _review(passed=False),
        _implementation(summary="Added the missing health route."),
        _review(passed=True),
        _performance(),
    )

    status, run = _run(tmp_path, script, options=_options(max_rounds=1))

    state = asyncio.run(run.state.load(IssueQueueState))
    assert state is not None
    board = IssueBoard(tmp_path / "issues.json")
    snapshot = {
        "status": status.value,
        "calls": [
            {
                "role": role,
                "prior_messages": len(history),
                "response": response.__name__ if response is not None else None,
            }
            for role, history, _message, response in script.calls
        ],
        "state": state.model_dump(mode="json"),
        "commits": [
            {
                "label": commit.label,
                "round_idx": commit.value.round_idx,
                "phase": commit.value.phase,
                "current_issue_id": commit.value.current_issue_id,
                "performance_records": len(commit.value.performance),
            }
            for commit in run.state.commits
            if isinstance(commit.value, IssueQueueState)
        ],
        "issues": [
            {
                "id": item.id,
                "type": item.type.value,
                "title": item.title,
                "status": item.status.value,
                "attempts": item.attempts,
                "created_by": item.created_by,
                "history": [
                    {
                        "actor": event.actor,
                        "action": event.action,
                        "iteration": event.iteration,
                        "note": event.note,
                        "summary": (event.payload or {}).get("summary"),
                        "verdict": (event.payload or {}).get("verdict"),
                    }
                    for event in item.history
                ],
            }
            for item in board.list()
        ],
        "logs": [call.message for call in run.observations.calls],
        "progress_headings": [
            line.removeprefix("## ")
            for line in (tmp_path / "progress.md").read_text(encoding="utf-8").splitlines()
            if line.startswith("## ")
        ],
        "tool_policy": json.loads(
            (tmp_path / ".vibesys" / "issue-tool-policy.json").read_text(encoding="utf-8")
        ),
    }
    expected = Path(__file__).with_name("fixtures") / "retry_trajectory.json"
    assert snapshot == json.loads(expected.read_text(encoding="utf-8"))


def test_paid_turn_failure_leaves_resumable_cursor_and_closes_sessions(tmp_path: Path) -> None:
    script = _Script(RuntimeError("provider unavailable"))

    async def scenario() -> FakeRun:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Build the candidate."),
            responder=script.respond,
            supported_extra_tools=_FAKE_AGENT_TOOLS,
            supported_agent_capabilities=_FAKE_AGENT_CAPABILITIES,
        )
        try:
            with pytest.raises(RuntimeError, match="provider unavailable"):
                await PLUGIN.orchestrate(run, _options())
            return run
        finally:
            await run.close()

    run = asyncio.run(scenario())

    state = asyncio.run(run.state.load(IssueQueueState))
    assert state is not None
    assert (state.round_idx, state.phase, state.current_issue_id) == (0, "implementer", 1)
    board = IssueBoard(tmp_path / "issues.json")
    assert board.list()[0].status is IssueStatus.IN_PROGRESS
    assert all(session.closed for session in run.agents.sessions)


def test_judge_crash_reopens_without_repeating_paid_implementation(tmp_path: Path) -> None:
    baseline_status, baseline_host = _run(
        tmp_path / "baseline",
        _Script(_implementation(), _review(passed=True), _performance()),
        options=_options(max_rounds=1),
    )
    baseline_state = asyncio.run(baseline_host.state.load(IssueQueueState))
    assert baseline_state is not None

    interrupted_script = _Script(_implementation(), RuntimeError("judge disconnected"))
    resumed_script = _Script(_review(passed=True), _performance())

    async def crash_and_reopen() -> tuple[RunStatus, FakeRun, FakeRun]:
        project_root = tmp_path / "reopened"
        interrupted = _fake_host(project_root, interrupted_script)
        try:
            with pytest.raises(RuntimeError, match="judge disconnected"):
                await PLUGIN.orchestrate(interrupted, _options(max_rounds=1))
            persisted = await interrupted.state.load(IssueQueueState)
            assert persisted is not None
            assert (persisted.round_idx, persisted.phase, persisted.current_issue_id) == (
                0,
                "judge",
                1,
            )
        finally:
            await interrupted.close()

        reopened = _fake_host(project_root, resumed_script)
        await reopened.state.commit(
            persisted,
            workspace=reopened.workspaces.root,
            label="reopen persisted issue-queue state",
        )
        try:
            status = await PLUGIN.orchestrate(reopened, _options(max_rounds=1))
            return status, interrupted, reopened
        finally:
            await reopened.close()

    resumed_status, interrupted, reopened = asyncio.run(crash_and_reopen())

    assert baseline_status is resumed_status is RunStatus.SUCCEEDED
    assert [call[0] for call in (*interrupted_script.calls, *resumed_script.calls)] == [
        "implementer",
        "judge",
        "judge",
        "perf_eval",
    ]
    resumed_state = asyncio.run(reopened.state.load(IssueQueueState))
    assert resumed_state == baseline_state
    issue = IssueBoard(tmp_path / "reopened" / "issues.json").get(1)
    assert issue is not None
    assert issue.status is IssueStatus.CLOSED
    assert issue.attempts == 1
    assert [event.actor for event in issue.history if event.action == "attempt"] == ["implementer"]
    assert all(session.closed for session in interrupted.agents.sessions)
    assert all(session.closed for session in reopened.agents.sessions)


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
    async def scenario() -> tuple[FakeRun, RuntimeError]:
        run = FakeRun(
            PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(domain_id="generic", objective="Build the candidate."),
            supported_extra_tools=_FAKE_AGENT_TOOLS,
            supported_agent_capabilities=_FAKE_AGENT_CAPABILITIES,
        )
        run.agents.script_creation(*creation_script)
        try:
            with pytest.raises(RuntimeError, match=f"{failed_role} unavailable") as raised:
                await PLUGIN.orchestrate(run, _options())
            return run, raised.value
        finally:
            await run.close()

    run, error = asyncio.run(scenario())

    assert error.args == (f"{failed_role} unavailable",)
    assert len(run.agents.sessions) == opened_sessions
    assert all(session.closed for session in run.agents.sessions)
    assert list(tmp_path.iterdir()) == []
    assert run.state.commits == ()


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
    assert [tool.id for tool in PERF_EVALUATOR.extra_tools] == [
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
