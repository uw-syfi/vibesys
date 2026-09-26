"""Public behavior of the plain single-agent orchestration plugin."""

from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.orchestrations.single import PLUGIN
from vibesys.orchestrations.single.models import PaidAttempt, SingleState
from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.search.hypothesis import OrchestratorPlan
from vs_runtime.api import AccuracyEvaluation, BenchmarkEvaluation, RunFacts, RunStatus
from vs_runtime.api.testing import FakeRunHost, FakeWorkspace

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole


DESIGNER, IMPLEMENTER = PLUGIN.agents


def _options(**changes: object) -> BaseModel:
    return PLUGIN.options.model_validate(
        {
            "interface": "service",
            "max_rounds": 2,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 10,
            "memory_layout": "directories",
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


def _response(**changes: object) -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse.model_validate(
        {
            "summary": "Implemented batching.",
            "expected_behavior": "Fewer launches.",
            "self_review": "Correctness checks passed.",
            "feedback": "",
            "verdict": Verdict.PASS,
            "bottlenecks": "Launch overhead.",
            "suggestions": "Try larger batches.",
            "profile_analysis": "Launch time fell.",
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


def test_plugin_declares_production_options_and_rejects_wrong_preset_polarity() -> None:
    assert PLUGIN.id == "single-agent"
    assert PLUGIN.agents == (DESIGNER, IMPLEMENTER)
    assert PLUGIN.state is SingleState

    with pytest.raises(ValidationError, match="interface"):
        _options(interface="socket")
    with pytest.raises(ValidationError, match="memory_layout"):
        _options(memory_layout="unknown")
    with pytest.raises(ValidationError, match="profile_guided"):
        _options(profile_guided={"min_measured_rounds": 2})
    with pytest.raises(ValidationError, match="unexpected_option"):
        _options(unexpected_option=True)


def test_multi_round_search_checkpoints_paid_turns_and_policy_files(tmp_path: Path) -> None:
    script = _Script(_plan("H-01"), _response(), _plan("H-02"), _response())

    status, host = _run(tmp_path, script)

    assert status is RunStatus.SUCCEEDED
    assert host.control.checkpoints == 2
    state = asyncio.run(host.state.load(SingleState))
    assert state is not None
    assert [record.hypothesis_id for record in state.search.rounds] == ["H-01", "H-02"]
    assert state.last_paid_attempt is None
    assert state.last_response == _response()
    recorded_markers = [
        commit.value.last_paid_attempt
        for commit in host.state.commits
        if isinstance(commit.value, SingleState) and commit.value.last_paid_attempt is not None
    ]
    markers = [
        marker
        for index, marker in enumerate(recorded_markers)
        if index == 0 or marker != recorded_markers[index - 1]
    ]
    assert markers == [
        PaidAttempt(
            round_number=1,
            role_id=IMPLEMENTER.id,
            member_id="H-01",
            turn_number=1,
        ),
        PaidAttempt(
            round_number=2,
            role_id=IMPLEMENTER.id,
            member_id="H-02",
            turn_number=1,
        ),
    ]
    assert (tmp_path / "roadmap" / "index.md").is_file()
    assert (tmp_path / "progress" / "plans" / "round-0001.json").is_file()
    assert (tmp_path / "progress" / "round-0002.md").is_file()
    assert [role for role, _history, _message in script.calls] == [
        DESIGNER.id,
        IMPLEMENTER.id,
        DESIGNER.id,
        IMPLEMENTER.id,
    ]


def test_official_evaluation_records_runtime_binding_and_selects_winner(
    tmp_path: Path,
) -> None:
    script = _Script(_plan("H-01"), _response())
    facts = RunFacts(
        domain_id="generic",
        accuracy_configured=True,
        benchmark_configured=True,
        accuracy_command="check-accuracy",
        benchmark_command="measure-throughput",
    )

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
            max_rounds=1,
            official_eval_every=1,
            metric_space=MetricSpace(objectives=(Objective(name="throughput", direction="max"),)),
        ),
        facts=facts,
        configure=configure,
    )

    assert status is RunStatus.SUCCEEDED
    state = asyncio.run(host.state.load(SingleState))
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


def test_failed_review_retries_in_one_named_session(tmp_path: Path) -> None:
    script = _Script(
        _plan("H-01"),
        _response(verdict=Verdict.FAIL, feedback="accuracy regressed"),
        _response(),
    )

    status, host = _run(tmp_path, script, options=_options(max_rounds=1))

    assert status is RunStatus.SUCCEEDED
    implementer_calls = [call for call in script.calls if call[0] == IMPLEMENTER.id]
    assert [len(history) for _role, history, _message in implementer_calls] == [0, 1]
    assert "accuracy regressed" in implementer_calls[1][2]
    implementer_sessions = [
        session for session in host.agents.sessions if session.role.id == IMPLEMENTER.id
    ]
    assert len(implementer_sessions) == 1
    assert implementer_sessions[0].member_id == "H-01"


def test_official_accuracy_failure_retries_with_feedback(tmp_path: Path) -> None:
    script = _Script(_plan("H-01"), _response(), _response())

    def configure(host: FakeRunHost) -> None:
        host.evaluation.script_accuracy(
            AccuracyEvaluation(executed=True, feedback="accuracy regressed"),
            AccuracyEvaluation(executed=True),
        )
        host.evaluation.script_benchmark(
            BenchmarkEvaluation(executed=True, metric_name="throughput", metric_value=80.0)
        )

    status, host = _run(
        tmp_path,
        script,
        options=_options(max_rounds=1, official_eval_every=1),
        facts=RunFacts(domain_id="generic", accuracy_configured=True, benchmark_configured=True),
        configure=configure,
    )

    assert status is RunStatus.SUCCEEDED
    assert len(host.evaluation.accuracy_calls) == 2
    assert len(host.evaluation.benchmark_calls) == 1
    implementer_calls = [call for call in script.calls if call[0] == IMPLEMENTER.id]
    assert [len(history) for _role, history, _message in implementer_calls] == [0, 1]
    assert "accuracy regressed" in implementer_calls[1][2]
    state = asyncio.run(host.state.load(SingleState))
    assert state is not None
    assert state.search.rounds[0].official_evaluation
    assert state.search.rounds[0].perf_provenance == "framework"


def test_paid_attempt_is_not_repeated_after_interrupted_turn(tmp_path: Path) -> None:
    async def scenario() -> tuple[FakeRunHost, _Script]:
        script = _Script(_plan("H-01"), RuntimeError("agent disconnected"), _response())
        host = FakeRunHost(PLUGIN, project_root=tmp_path, responder=script.respond)
        try:
            with pytest.raises(RuntimeError, match="agent disconnected"):
                await PLUGIN.orchestrate(host, _options(max_rounds=1))
            assert all(session.closed for session in host.agents.sessions)
            interrupted = await host.state.load(SingleState)
            assert interrupted is not None
            assert interrupted.last_paid_attempt == PaidAttempt(
                round_number=1,
                role_id=IMPLEMENTER.id,
                member_id="H-01",
                turn_number=1,
            )
            assert await PLUGIN.orchestrate(host, _options(max_rounds=1)) is RunStatus.SUCCEEDED
            assert all(session.closed for session in host.agents.sessions)
            return host, script
        finally:
            await host.close()

    host, script = asyncio.run(scenario())
    assert [role for role, _history, _message in script.calls] == [
        DESIGNER.id,
        IMPLEMENTER.id,
        IMPLEMENTER.id,
    ]
    sessions = [session for session in host.agents.sessions if session.role.id == IMPLEMENTER.id]
    assert [session.member_id for session in sessions] == ["H-01", "H-01"]
    assert all(session.closed for session in host.agents.sessions)
    state = asyncio.run(host.state.load(SingleState))
    assert state is not None
    assert state.last_paid_attempt is None
    assert len(state.search.rounds) == 1


def test_rollback_uses_recorded_parent_and_sessions_close(tmp_path: Path) -> None:
    script = _Script(
        _plan("H-01"),
        _response(),
        _plan("H-02", revert_to_round=1),
        _response(),
    )

    status, host = _run(tmp_path, script)

    assert status is RunStatus.SUCCEEDED
    state = asyncio.run(host.state.load(SingleState))
    assert state is not None
    first, _second = state.search.rounds
    hypothesis = state.search.by_id("H-02")
    assert hypothesis is not None
    assert hypothesis.revert_applied
    assert hypothesis.revert_commit == first.commit
    assert hypothesis.parent_commit == first.commit
    assert all(session.closed for session in host.agents.sessions)


def test_no_trusted_winner_restores_baseline(tmp_path: Path) -> None:
    script = _Script(_plan("H-01"), _response(verdict=Verdict.FAIL, feedback="broken"))

    status, host = _run(tmp_path, script, options=_options(max_rounds=1, max_retries_per_round=1))

    assert status is RunStatus.SUCCEEDED
    workspace = host.workspaces.root
    assert isinstance(workspace, FakeWorkspace)
    assert workspace.retained == {}
    assert "no trusted winner; restored the input baseline" in host.logs
    state = asyncio.run(host.state.load(SingleState))
    assert state is not None
    assert not state.search.rounds[0].passed


def test_selected_profiler_support_name_and_agent_metric_provenance(tmp_path: Path) -> None:
    script = _Script(
        _plan("H-01"),
        _response(perf_metric=90.0, perf_unit="throughput"),
    )

    status, host = _run(
        tmp_path,
        script,
        options=_options(max_rounds=1, official_eval_every=1),
        facts=RunFacts(domain_id="generic", profiler_id="linux_cpu"),
    )

    assert status is RunStatus.SUCCEEDED
    implementer_prompt = next(
        message for role, _history, message in script.calls if role == IMPLEMENTER.id
    )
    assert "linux_cpu_profiler" in implementer_prompt
    state = asyncio.run(host.state.load(SingleState))
    assert state is not None
    assert state.search.rounds[0].perf_provenance == "implementer"


def test_file_memory_layout_owns_plain_policy_artifacts(tmp_path: Path) -> None:
    script = _Script(_plan("H-01"), _response())

    status, _host = _run(tmp_path, script, options=_options(max_rounds=1, memory_layout="files"))

    assert status is RunStatus.SUCCEEDED
    assert (tmp_path / "roadmap.md").is_file()
    assert (tmp_path / "progress.md").is_file()
    assert (tmp_path / "progress-artifacts" / "plans" / "round-0001.json").is_file()
