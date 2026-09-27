"""Public behavior of the profile-guided multi-agent plugin preset."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.orchestration.multi import PLUGIN, PROFILE_GUIDED_PLUGIN
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.orchestration.multi.models import MultiState
from vibesys.schemas import Verdict
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.search.profile_focus import ProfileAttributionError, ProfileGuidanceStatus
from vs_runtime.api import BenchmarkEvaluation, CommandResult, RunFacts, RunStatus
from vs_runtime.api.testing import FakeRunHost

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

DESIGNER, PROFILER, IMPLEMENTER, JUDGE = PROFILE_GUIDED_PLUGIN.agents
_COMPONENT = "prefill_launch_overhead"


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


def _options(**changes: object) -> BaseModel:
    return PROFILE_GUIDED_PLUGIN.options.model_validate(
        {
            "interface": "service",
            "max_rounds": 2,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 100,
            "memory_layout": "directories",
            "metric_space": MetricSpace(
                objectives=(Objective(name="throughput", direction="max"),)
            ),
            "profile_guided": ProfileGuidedInput(
                command=("python", "attribute.py"),
                timeout_seconds=73,
                min_measured_rounds=1,
                min_relative_improvement=0.5,
            ),
            **changes,
        }
    )


def _pre_round() -> PreRoundDecision:
    return PreRoundDecision(
        need_profile=False,
        profile_focus="",
        reasoning="Existing evidence is sufficient.",
    )


def _plan(hypothesis_id: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        hypothesis="Batching removes per-request launch overhead.",
        title="Batch prefill",
        task="Batch prefill requests.",
        pass_criteria="Throughput improves without an accuracy regression.",  # noqa: S106  # lint-waiver: LW-920448 [S106]; fixture text is not a credential.
        reasoning="The trace shows repeated launch overhead.",
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


def _judge() -> JudgeResponse:
    return JudgeResponse(
        analysis="The change matches the plan and evidence.",
        feedback="",
        verdict=Verdict.PASS,
    )


def _attribution_payload(cost: float) -> str:
    return json.dumps(
        {
            "version": 1,
            "cost_unit": "ms",
            "components": [
                {
                    "name": _COMPONENT,
                    "cost": cost,
                    "share": 0.4,
                    "evidence": ["profile.json:12"],
                }
            ],
        }
    )


def _script_attribution(host: FakeRunHost, *costs: float) -> None:
    for cost in costs:
        host.commands.script(CommandResult(output=_attribution_payload(cost), exit_code=0))


def _benchmark() -> BenchmarkEvaluation:
    return BenchmarkEvaluation(
        executed=True,
        metric_name="throughput",
        metric_value=100.0,
        metric_unit="requests/s",
        row={"throughput": 100.0},
    )


def test_profile_preset_declares_required_profile_options() -> None:
    assert PROFILE_GUIDED_PLUGIN.id == "profile-guided-multi-agent"
    assert PROFILE_GUIDED_PLUGIN.agents == PLUGIN.agents
    assert PROFILE_GUIDED_PLUGIN.state is PLUGIN.state

    values = _options().model_dump()
    values.pop("profile_guided")
    with pytest.raises(ValidationError, match="profile_guided"):
        PROFILE_GUIDED_PLUGIN.options.model_validate(values)
    with pytest.raises(ValidationError, match="profile_guided"):
        PROFILE_GUIDED_PLUGIN.options.model_validate({**values, "profile_guided": None})
    with pytest.raises(ValidationError, match="profile_guided"):
        PLUGIN.options.model_validate(_options().model_dump())


def test_profile_guidance_persists_without_changing_official_cadence(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[RunStatus, FakeRunHost, _Script]:
        script = _Script(
            _pre_round(),
            _plan("H-01"),
            _implementation(),
            _judge(),
            _pre_round(),
            _plan("H-02"),
            _implementation(),
            _judge(),
        )
        host = FakeRunHost(
            PROFILE_GUIDED_PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve the candidate.",
                benchmark_configured=True,
            ),
            responder=script.respond,
        )
        _script_attribution(host, 40.0, 35.0)
        host.evaluation.script_benchmark(_benchmark())
        try:
            status = await PROFILE_GUIDED_PLUGIN.orchestrate(host, _options())
            return status, host, script
        finally:
            await host.close()

    status, host, script = asyncio.run(scenario())

    assert status is RunStatus.SUCCEEDED
    designer_prompts = [message for role, _history, message in script.calls if role == DESIGNER.id]
    implementer_prompts = [
        message for role, _history, message in script.calls if role == IMPLEMENTER.id
    ]
    judge_prompts = [message for role, _history, message in script.calls if role == JUDGE.id]
    assert _COMPONENT in designer_prompts[1]
    assert "Profile-guided focus" in designer_prompts[1]
    assert _COMPONENT in implementer_prompts[0]
    assert "profile-guided component measurement" in implementer_prompts[0]
    assert "profile-guided component measurement" in judge_prompts[0]

    state = asyncio.run(host.state.load(MultiState))
    assert state is not None
    assert [record.official_evaluation for record in state.search.rounds] == [False, True]
    assert len(host.evaluation.accuracy_calls) == 1
    focus = state.search.profile_guidance
    assert focus is not None
    assert focus.active_component == _COMPONENT
    assert focus.components[0].status is ProfileGuidanceStatus.ACTIVE
    assert [sample.round for sample in focus.components[0].attribution_history] == [1, 2]
    assert focus.components[0].improvement_history == []

    assert len(host.commands.calls) == 2
    assert all(call.argv == ("python", "attribute.py") for call in host.commands.calls)
    assert all(call.output_argument == "--vs-output" for call in host.commands.calls)
    assert all(call.timeout_seconds == 73 for call in host.commands.calls)
    assert [session.member_id for session in host.agents.sessions] == [
        None,
        None,
        "H-01",
        None,
        None,
        None,
        "H-02",
        None,
    ]
    assert all(session.closed for session in host.agents.sessions)


def test_continuation_reuses_implementer_without_reprofiling_or_replanning(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[FakeRunHost, _Script]:
        script = _Script(
            _pre_round(),
            _plan("H-01"),
            _implementation(hypothesis_outcome="continue", next_step="Measure a larger batch."),
            _judge(),
            _implementation(),
            _judge(),
        )
        host = FakeRunHost(
            PROFILE_GUIDED_PLUGIN,
            project_root=tmp_path,
            facts=RunFacts(
                domain_id="generic",
                objective="Improve the candidate.",
                benchmark_configured=True,
            ),
            responder=script.respond,
        )
        _script_attribution(host, 40.0)
        host.evaluation.script_benchmark(_benchmark())
        try:
            assert await PROFILE_GUIDED_PLUGIN.orchestrate(host, _options()) is RunStatus.SUCCEEDED
            return host, script
        finally:
            await host.close()

    host, script = asyncio.run(scenario())

    assert len(host.commands.calls) == 1
    assert [role for role, _history, _message in script.calls] == [
        DESIGNER.id,
        DESIGNER.id,
        IMPLEMENTER.id,
        JUDGE.id,
        IMPLEMENTER.id,
        JUDGE.id,
    ]
    implementer_calls = [call for call in script.calls if call[0] == IMPLEMENTER.id]
    assert [len(history) for _role, history, _message in implementer_calls] == [0, 1]
    implementer_sessions = [
        session for session in host.agents.sessions if session.role.id == IMPLEMENTER.id
    ]
    assert len(implementer_sessions) == 1
    assert implementer_sessions[0].member_id == "H-01"
    judge_sessions = [session for session in host.agents.sessions if session.role.id == JUDGE.id]
    assert len(judge_sessions) == 2
    assert all(not session.history[:-1] for session in judge_sessions)
    state = asyncio.run(host.state.load(MultiState))
    assert state is not None
    focus = state.search.profile_guidance
    assert focus is not None
    assert [sample.round for sample in focus.components[0].attribution_history] == [1]


def test_profile_command_failure_is_typed_and_opens_no_agent_sessions(
    tmp_path: Path,
) -> None:
    async def scenario() -> FakeRunHost:
        host = FakeRunHost(PROFILE_GUIDED_PLUGIN, project_root=tmp_path)
        host.commands.script(CommandResult(output="profiler failed", exit_code=2))
        try:
            with pytest.raises(ProfileAttributionError, match="exit code 2"):
                await PROFILE_GUIDED_PLUGIN.orchestrate(host, _options(max_rounds=1))
        finally:
            await host.close()
        return host

    host = asyncio.run(scenario())

    assert len(host.commands.calls) == 1
    assert host.commands.calls[0].output_argument == "--vs-output"
    assert host.agents.sessions == ()
