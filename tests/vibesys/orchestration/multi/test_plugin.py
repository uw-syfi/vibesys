"""Public behavior of the plain multi-agent orchestration plugin."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.errors import InvalidPlanError
from vibesys.orchestration.hypothesis import OrchestratorPlan
from vibesys.orchestration.metrics import MetricSpace, Objective
from vibesys.orchestration.multi import PLUGIN
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.orchestration.multi.models import MultiState, PaidAttempt
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.review import Verdict
from vs_runtime.api import (
    BenchmarkEvaluation,
    LocalValidationEvaluation,
    RunFacts,
    RunStatus,
)
from vs_runtime.api.testing import FakeRunHost, FakeWorkspace

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


DESIGNER, PROFILER, IMPLEMENTER, JUDGE = PLUGIN.agents


def _options(**changes: object) -> BaseModel:
    return PLUGIN.options.model_validate(
        {
            "interface": "service",
            "max_rounds": 1,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 10,
            **changes,
        }
    )


def _pre_round(**changes: object) -> PreRoundDecision:
    return PreRoundDecision.model_validate(
        {
            "need_profile": False,
            "profile_focus": "",
            "reasoning": "Existing evidence is sufficient.",
            **changes,
        }
    )


def _plan(hypothesis_id: str, **changes: object) -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "hypothesis_id": hypothesis_id,
            "hypothesis": "Batching removes per-request overhead.",
            "title": "Batch prefill",
            "task": "Batch prefill requests.",
            "pass_criteria": "Throughput improves without an accuracy regression.",
            "reasoning": "The trace shows repeated launch overhead.",
            **changes,
        }
    )


def _implementation(**changes: object) -> ImplementerResponse:
    return ImplementerResponse.model_validate(
        {
            "summary": "Implemented batching.",
            "expected_behavior": "Fewer launches.",
            "hypothesis_outcome": "nominated",
            "evidence": "The local smoke check passed.",
            **changes,
        }
    )


def _judge(**changes: object) -> JudgeResponse:
    return JudgeResponse.model_validate(
        {
            "analysis": "The change matches the plan and evidence.",
            "feedback": "",
            "verdict": Verdict.PASS,
            **changes,
        }
    )


class _Script:
    def __init__(self, *replies: object) -> None:
        self.replies = deque(replies)
        self.calls: list[tuple[str, tuple[str, ...], str]] = []

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        self.calls.append((role.id, history, message))
        reply = self.replies.popleft()
        if isinstance(reply, BaseException):
            raise reply
        return reply


def _run(
    path: Path,
    script: _Script,
    *,
    options: BaseModel | None = None,
    facts: RunFacts | None = None,
    configure: Callable[[FakeRunHost], None] | None = None,
) -> tuple[RunStatus, FakeRunHost]:
    async def scenario() -> tuple[RunStatus, FakeRunHost]:
        host = FakeRunHost(
            PLUGIN,
            project_root=path,
            facts=facts,
            responder=script.respond,
        )
        if configure is not None:
            configure(host)
        try:
            status = await PLUGIN.orchestrate(host, options or _options())
            return status, host
        finally:
            await host.close()

    return asyncio.run(scenario())


def test_plugin_declares_four_roles_and_plain_production_options() -> None:
    assert PLUGIN.id == "multi-agent"
    assert PLUGIN.agents == (DESIGNER, PROFILER, IMPLEMENTER, JUDGE)
    assert PLUGIN.state is MultiState

    with pytest.raises(ValidationError, match="interface"):
        _options(interface="socket")
    for removed_layout in ("files", "directories"):
        with pytest.raises(ValidationError, match="memory_layout"):
            _options(memory_layout=removed_layout)
    with pytest.raises(ValidationError, match="profile_guided"):
        _options(profile_guided={"min_measured_rounds": 2})
    with pytest.raises(ValidationError, match="unexpected_option"):
        _options(unexpected_option=True)


def test_round_uses_fresh_policy_sessions_and_named_implementer(tmp_path: Path) -> None:
    script = _Script(_pre_round(), _plan("H-01"), _implementation(), _judge())

    status, host = _run(tmp_path, script)

    assert status is RunStatus.SUCCEEDED
    assert host.control.checkpoints == 1
    assert [role for role, _history, _message in script.calls] == [
        DESIGNER.id,
        DESIGNER.id,
        IMPLEMENTER.id,
        JUDGE.id,
    ]
    sessions = host.agents.sessions
    assert [session.member_id for session in sessions] == [None, None, "H-01", None]
    assert sessions[0] is not sessions[1]
    assert sessions[0].writable_paths == ("roadmap/index.md",)
    assert sessions[1].writable_paths == ("roadmap/index.md",)
    assert all(session.closed for session in sessions)
    state = asyncio.run(host.state.load(MultiState))
    assert state is not None
    assert [record.hypothesis_id for record in state.search.rounds] == ["H-01"]
    assert state.last_paid_attempt is None
    assert (tmp_path / "progress" / "plans" / "round-0001.json").is_file()
    assert (tmp_path / "progress" / "evidence" / "round-0001-attempt-01-implementer.json").is_file()


def test_failed_judge_retries_same_implementer_context_with_fresh_judge(
    tmp_path: Path,
) -> None:
    script = _Script(
        _pre_round(),
        _plan("H-01"),
        _implementation(),
        _judge(verdict=Verdict.FAIL, feedback="batch boundary is unchecked"),
        _implementation(evidence="Added and ran the boundary check."),
        _judge(),
    )

    status, host = _run(tmp_path, script)

    assert status is RunStatus.SUCCEEDED
    implementer_calls = [call for call in script.calls if call[0] == IMPLEMENTER.id]
    assert [len(history) for _role, history, _message in implementer_calls] == [0, 1]
    assert "batch boundary is unchecked" in implementer_calls[1][2]
    implementer_sessions = [
        session for session in host.agents.sessions if session.role.id == IMPLEMENTER.id
    ]
    judge_sessions = [session for session in host.agents.sessions if session.role.id == JUDGE.id]
    assert len(implementer_sessions) == 1
    assert implementer_sessions[0].member_id == "H-01"
    assert len(judge_sessions) == 2
    assert all(not session.history[:-1] for session in judge_sessions)


def test_invalid_plan_correction_continues_in_planning_session(tmp_path: Path) -> None:
    rejected = _plan(
        "H-01",
        hypothesis_updates=[
            {
                "hypothesis_id": "H-01",
                "disposition": "abandoned",
                "reason": "Self-reference is invalid.",
            }
        ],
    )
    script = _Script(
        _pre_round(),
        rejected,
        _plan("H-02"),
        _implementation(),
        _judge(),
    )

    status, host = _run(tmp_path, script)

    assert status is RunStatus.SUCCEEDED
    designer_calls = [call for call in script.calls if call[0] == DESIGNER.id]
    assert [len(history) for _role, history, _message in designer_calls] == [0, 0, 1]
    assert "never reuse an identifier" in designer_calls[2][2]
    designer_sessions = [
        session for session in host.agents.sessions if session.role.id == DESIGNER.id
    ]
    assert len(designer_sessions) == 2
    assert len(designer_sessions[1].history) == 2


def test_invalid_plan_correction_exhaustion_fails_without_more_agent_work(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[FakeRunHost, _Script]:
        script = _Script(
            _pre_round(),
            _plan("H-01"),
            _implementation(),
            _judge(),
            _pre_round(),
            _plan("H-01"),
            _plan("H-01"),
        )
        host = FakeRunHost(PLUGIN, project_root=tmp_path, responder=script.respond)
        try:
            with pytest.raises(InvalidPlanError):
                await PLUGIN.orchestrate(host, _options(max_rounds=2))
            return host, script
        finally:
            await host.close()

    host, script = asyncio.run(scenario())

    assert [role for role, _history, _message in script.calls] == [
        DESIGNER.id,
        DESIGNER.id,
        IMPLEMENTER.id,
        JUDGE.id,
        DESIGNER.id,
        DESIGNER.id,
        DESIGNER.id,
    ]
    assert all(session.closed for session in host.agents.sessions)


def test_official_evaluation_records_binding_and_selects_winner(tmp_path: Path) -> None:
    script = _Script(_pre_round(), _plan("H-01"), _implementation(), _judge())

    def configure(host: FakeRunHost) -> None:
        host.evaluation.script_benchmark(
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=120.0,
                metric_direction="max",
                metric_unit="requests/s",
                row={"throughput": 120.0},
            )
        )

    status, host = _run(
        tmp_path,
        script,
        options=_options(
            official_eval_every=1,
            metric_space=MetricSpace(objectives=(Objective(name="throughput", direction="max"),)),
        ),
        facts=RunFacts(
            domain_id="generic",
            objective="Improve the candidate.",
            accuracy_configured=True,
            benchmark_configured=True,
        ),
        configure=configure,
    )

    assert status is RunStatus.SUCCEEDED
    state = asyncio.run(host.state.load(MultiState))
    assert state is not None
    record = state.search.rounds[0]
    assert record.official_evaluation
    assert record.perf_metric == 120.0
    assert record.perf_provenance == "framework"
    assert record.implementer_driver == "fake"
    assert host.evaluation.accuracy_calls
    assert host.evaluation.benchmark_calls[0].objectives[0].name == "throughput"
    workspace = host.workspaces.root
    assert isinstance(workspace, FakeWorkspace)
    assert workspace.retained == {"selected-round-0001": record.commit}


def test_judge_approved_local_validation_failure_retries_with_report(
    tmp_path: Path,
) -> None:
    script = _Script(
        _pre_round(),
        _plan("H-01"),
        _implementation(validation_recipe_artifact="checks/recipe.json"),
        _judge(),
        _implementation(validation_recipe_artifact="checks/recipe.json"),
        _judge(),
    )

    def configure(host: FakeRunHost) -> None:
        host.evaluation.script_local_validation(
            LocalValidationEvaluation(
                passed=False,
                feedback=(
                    "Framework local validation failed for 'imports': missing module. "
                    "Inspect `progress/validation/round-0001-attempt-01.json`."
                ),
                report_location="progress/validation/round-0001-attempt-01.json",
            ),
            LocalValidationEvaluation(
                passed=True,
                report_location="progress/validation/round-0001-attempt-02.json",
            ),
        )

    status, host = _run(tmp_path, script, configure=configure)

    assert status is RunStatus.SUCCEEDED
    assert [call.recipe_artifact for call in host.evaluation.local_validation_calls] == [
        "checks/recipe.json",
        "checks/recipe.json",
    ]
    assert [call.report_location for call in host.evaluation.local_validation_calls] == [
        "progress/validation/round-0001-attempt-01.json",
        "progress/validation/round-0001-attempt-02.json",
    ]
    implementer_calls = [call for call in script.calls if call[0] == IMPLEMENTER.id]
    assert "missing module" in implementer_calls[1][2]


def test_profiler_is_fresh_and_bounded_to_round_evidence(tmp_path: Path) -> None:
    script = _Script(
        _pre_round(need_profile=True, profile_focus="CPU dispatch"),
        ProfilerSummary(
            analysis="Dispatch dominates.",
            bottlenecks="Python dispatch.",
            suggestions="Batch dispatches.",
        ),
        _plan("H-01"),
        _implementation(),
        _judge(),
    )

    status, host = _run(
        tmp_path,
        script,
        facts=RunFacts(
            domain_id="generic",
            objective="Improve the candidate.",
            profiler_id="linux_cpu",
        ),
    )

    assert status is RunStatus.SUCCEEDED
    profiler_sessions = [
        session for session in host.agents.sessions if session.role.id == PROFILER.id
    ]
    assert len(profiler_sessions) == 1
    assert profiler_sessions[0].member_id is None
    assert profiler_sessions[0].writable_paths == ("progress/profiles/round-0001",)
    plan_prompt = [message for role, _history, message in script.calls if role == DESIGNER.id][1]
    assert "fresh profiler result is recorded" in plan_prompt
    assert "Dispatch dominates" in (tmp_path / "progress" / "round-0001.md").read_text()


def test_paid_attempt_is_not_replayed_after_interrupted_turn(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRunHost, _Script]:
        script = _Script(
            _pre_round(),
            _plan("H-01"),
            RuntimeError("agent disconnected"),
            _implementation(),
            _judge(),
        )
        host = FakeRunHost(PLUGIN, project_root=tmp_path, responder=script.respond)
        try:
            with pytest.raises(RuntimeError, match="agent disconnected"):
                await PLUGIN.orchestrate(host, _options())
            interrupted = await host.state.load(MultiState)
            assert interrupted is not None
            assert interrupted.last_paid_attempt == PaidAttempt(
                round_number=1,
                member_id="H-01",
                turn_number=1,
            )
            assert await PLUGIN.orchestrate(host, _options()) is RunStatus.SUCCEEDED
            return host, script
        finally:
            await host.close()

    host, script = asyncio.run(scenario())
    assert [role for role, _history, _message in script.calls] == [
        DESIGNER.id,
        DESIGNER.id,
        IMPLEMENTER.id,
        IMPLEMENTER.id,
        JUDGE.id,
    ]
    implementer_sessions = [
        session for session in host.agents.sessions if session.role.id == IMPLEMENTER.id
    ]
    assert [session.member_id for session in implementer_sessions] == ["H-01", "H-01"]
    assert all(session.closed for session in host.agents.sessions)


def test_rollback_uses_recorded_parent_and_closes_sessions(tmp_path: Path) -> None:
    script = _Script(
        _pre_round(),
        _plan("H-01"),
        _implementation(),
        _judge(),
        _pre_round(),
        _plan("H-02", revert_to_round=1),
        _implementation(),
        _judge(),
    )

    status, host = _run(tmp_path, script, options=_options(max_rounds=2))

    assert status is RunStatus.SUCCEEDED
    state = asyncio.run(host.state.load(MultiState))
    assert state is not None
    first, _second = state.search.rounds
    hypothesis = state.search.by_id("H-02")
    assert hypothesis is not None
    assert hypothesis.revert_applied
    assert hypothesis.revert_commit == first.commit
    assert hypothesis.parent_commit == first.commit
    assert all(session.closed for session in host.agents.sessions)
