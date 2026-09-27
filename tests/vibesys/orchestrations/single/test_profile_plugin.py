"""Public behavior of the profile-guided single-agent plugin preset."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.orchestrations.single import PLUGIN, PROFILE_GUIDED_PLUGIN
from vibesys.roles.common import Verdict
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.search.hypothesis import OrchestratorPlan
from vibesys.search.profile_focus import ProfileAttributionError, ProfileGuidanceStatus
from vs_runtime.api import BenchmarkEvaluation, CommandResult, RunFacts, RunStatus
from vs_runtime.api.testing import FakeRunHost

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole

DESIGNER, IMPLEMENTER = PROFILE_GUIDED_PLUGIN.agents
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
            "interface": "inprocess",
            "max_rounds": 2,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": 100,
            "memory_layout": "files",
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


def _plan(hypothesis_id: str) -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id=hypothesis_id,
        hypothesis="Batching removes per-request launch overhead.",
        title="Batch prefill",
        task="Batch prefill requests.",
        pass_criteria="Throughput improves without an accuracy regression.",  # noqa: S106  # lint-waiver: LW-920450 [S106]; fixture text is not a credential.
        reasoning="The trace shows repeated launch overhead.",
    )


def _response() -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="Implemented batching.",
        expected_behavior="Fewer launches.",
        self_review="Correctness checks passed.",
        feedback="",
        verdict=Verdict.PASS,
        bottlenecks="Launch overhead.",
        suggestions="Try larger batches.",
        profile_analysis="Launch time fell.",
    )


def _attribution_payload(*, cost: float) -> str:
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
        host.commands.script(
            CommandResult(output=_attribution_payload(cost=cost), exit_code=0),
        )


def test_profile_preset_declares_required_profile_options() -> None:
    assert PROFILE_GUIDED_PLUGIN.id == "profile-guided-single-agent"
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


def test_profile_guidance_drives_prompts_and_persists_focus_without_changing_cadence(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[RunStatus, FakeRunHost, _Script]:
        script = _Script(_plan("H-01"), _response(), _plan("H-02"), _response())
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
        host.evaluation.script_benchmark(
            BenchmarkEvaluation(
                executed=True,
                metric_name="throughput",
                metric_value=100.0,
                metric_unit="requests/s",
                row={"throughput": 100.0},
            ),
        )
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
    assert _COMPONENT in designer_prompts[0]
    assert "Profile-guided focus" in designer_prompts[0]
    assert "profile-guided component measurement" in implementer_prompts[0]

    state_type = PROFILE_GUIDED_PLUGIN.state
    assert state_type is not None
    state = asyncio.run(host.state.load(state_type))
    assert state is not None
    dumped = state.model_dump()
    records = [
        record for hypothesis in dumped["search"]["hypotheses"] for record in hypothesis["rounds"]
    ]
    assert records[0]["official_evaluation"] is False
    assert records[1]["official_evaluation"] is True
    assert len(host.evaluation.accuracy_calls) == 1
    focus = dumped["search"]["profile_guidance"]
    assert focus["active_component"] == _COMPONENT
    component = focus["components"][0]
    assert component["name"] == _COMPONENT
    assert component["status"] == ProfileGuidanceStatus.ACTIVE
    assert [sample["round"] for sample in component["attribution_history"]] == [1, 2]
    assert component["improvement_history"] == []

    assert len(host.commands.calls) == 2
    first_run = host.commands.calls[0]
    assert first_run.argv == ("python", "attribute.py")
    assert first_run.output_argument == "--vs-output"
    assert first_run.timeout_seconds == 73
    assert [
        commit.label for commit in host.state.commits if commit.label and "prepare" in commit.label
    ] == [
        "profile-guided: prepare round 1",
        "profile-guided: prepare round 2",
    ]


def test_profile_command_failure_is_typed(tmp_path: Path) -> None:
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
