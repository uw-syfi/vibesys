"""Exact public-output goldens for the multi orchestration presets."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.golden.helpers import (
    assert_board_snapshot,
    assert_prompt_snapshot,
    prompt_text,
)

from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace, Objective
from vibesys.evaluators.perf_reply import ProfilerSummary
from vibesys.orchestration.multi import PLUGIN, PROFILE_GUIDED_PLUGIN
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.schemas import Verdict
from vibesys.search.hypothesis import OrchestratorPlan
from vs_runtime.api import (
    AccuracyEvaluation,
    BenchmarkEvaluation,
    CommandResult,
    RunFacts,
    RunStatus,
)
from vs_runtime.api.testing import FakeRunHost

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vs_runtime.api import AgentRole, OrchestrationPlugin


def _pre_round(*, profile: bool = False) -> PreRoundDecision:
    return PreRoundDecision(
        need_profile=profile,
        profile_focus="prefill kernels" if profile else "",
        reasoning="Profile the suspected bottleneck."
        if profile
        else "Existing evidence is sufficient.",
    )


def _plan(hypothesis_id: str = "H-01", **changes: object) -> OrchestratorPlan:
    return OrchestratorPlan.model_validate(
        {
            "hypothesis_id": hypothesis_id,
            "hypothesis": "Batching removes per-request launch overhead.",
            "title": "Batch prefill",
            "task": "Batch prefill requests.",
            "pass_criteria": "Throughput improves without an accuracy regression.",
            "reasoning": "The trace shows repeated launch overhead.",
            **changes,
        }
    )


def _implementation(summary: str = "Implemented batching.") -> ImplementerResponse:
    return ImplementerResponse(
        summary=summary,
        expected_behavior="Fewer launches.",
        evidence="The local smoke check passed.",
    )


def _judge(verdict: Verdict = Verdict.PASS, feedback: str = "") -> JudgeResponse:
    return JudgeResponse(
        analysis="The change matches the plan and evidence.",
        feedback=feedback,
        verdict=verdict,
    )


def _profiler() -> ProfilerSummary:
    return ProfilerSummary(
        analysis="Prefill dominates step time.",
        bottlenecks="Per-request prefill launch.",
        suggestions="Batch prefill requests.",
        perf_metric=1000.0,
        perf_unit="tokens/s",
    )


def _attribution() -> str:
    return json.dumps(
        {
            "version": 1,
            "cost_unit": "ms",
            "components": [
                {
                    "name": "prefill_launch_overhead",
                    "cost": 40.0,
                    "share": 0.4,
                    "evidence": ["profile.json:12"],
                }
            ],
        }
    )


class _Script:
    def __init__(self, *replies: object) -> None:
        self.replies = deque(replies)
        self.calls: list[dict[str, object]] = []

    def respond(
        self,
        role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        self.calls.append(
            {
                "role": role.id,
                "system_prompt": role.system_prompt,
                "history": list(history),
                "message": message,
                "response_schema": response.__name__ if response is not None else None,
            }
        )
        return self.replies.popleft()


def _plain_options(*, rounds: int = 1, official_every: int = 10) -> BaseModel:
    return PLUGIN.options.model_validate(
        {
            "interface": "service",
            "max_rounds": rounds,
            "max_retries_per_round": 2,
            "judge_every": 1,
            "official_eval_every": official_every,
            "memory_layout": "directories",
            "metric_space": MetricSpace(
                objectives=(Objective(name="throughput", direction="max"),)
            ),
        }
    )


def _profile_options(*, rounds: int = 1, official_every: int = 10) -> BaseModel:
    return PROFILE_GUIDED_PLUGIN.options.model_validate(
        {
            **_plain_options(rounds=rounds, official_every=official_every).model_dump(),
            "profile_guided": ProfileGuidedInput(
                command=("python", "attribute.py"),
                timeout_seconds=73,
                min_measured_rounds=1,
                min_relative_improvement=0.5,
            ),
        }
    )


def _scenario(name: str) -> tuple[object, ...]:
    if name == "retry_then_pass":
        return (
            _pre_round(),
            _plan(),
            _implementation("Partial batching."),
            _judge(Verdict.FAIL, "Batching does not cover decode."),
            _implementation("Completed batching after review."),
            _judge(),
        )
    if name == "profile":
        return _pre_round(profile=True), _profiler(), _plan(), _implementation(), _judge()
    if name == "rollback":
        return (
            _pre_round(),
            _plan(),
            _implementation("Round one batching."),
            _judge(),
            _pre_round(),
            _plan("H-02", revert_to_round=1),
            _implementation("Round two batching after rollback."),
            _judge(),
        )
    return _pre_round(), _plan(), _implementation(), _judge()


def _artifact_snapshot(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "recipe-schema.json"
    }


def _canonical_text(value: str) -> str:
    """Remove insignificant line-end whitespace from reviewed fixtures."""
    return "\n".join(line.rstrip() for line in value.rstrip().splitlines()) + "\n"


def _assert_golden(
    preset: str,
    scenario: str,
    observed: dict[str, object],
    *,
    workspace: Path,
) -> None:
    calls = observed["calls"]
    assert isinstance(calls, list)
    for index, call in enumerate(calls, start=1):
        assert isinstance(call, dict)
        role = call["role"]
        system_prompt = call["system_prompt"]
        message = call["message"]
        assert isinstance(role, str)
        assert isinstance(system_prompt, str)
        assert isinstance(message, str)
        assert_prompt_snapshot(
            preset,
            f"{index:02d}-{role}",
            scenario,
            _canonical_text(prompt_text(system_prompt, message)),
            workspace=workspace,
        )

    artifacts = observed["artifacts"]
    assert isinstance(artifacts, dict)
    for relative, content in artifacts.items():
        assert isinstance(relative, str)
        assert isinstance(content, str)
        assert_board_snapshot(
            preset,
            scenario,
            relative,
            _canonical_text(content),
            workspace=workspace,
        )

    projection = observed["projection"]
    retained = observed["retained"]
    assert_board_snapshot(
        preset,
        scenario,
        "_projection.json",
        json.dumps(projection, indent=2, sort_keys=True) + "\n",
        workspace=workspace,
    )
    assert_board_snapshot(
        preset,
        scenario,
        "_retained.json",
        json.dumps(retained, indent=2, sort_keys=True) + "\n",
        workspace=workspace,
    )


@pytest.mark.parametrize(
    ("plugin", "preset", "scenario"),
    [
        (PLUGIN, "multi", "pass"),
        (PLUGIN, "multi", "retry_then_pass"),
        (PLUGIN, "multi", "gate"),
        (PLUGIN, "multi", "profile"),
        (PLUGIN, "multi", "rollback"),
        (PROFILE_GUIDED_PLUGIN, "profile_multi", "pass"),
        (PROFILE_GUIDED_PLUGIN, "profile_multi", "retry_then_pass"),
        (PROFILE_GUIDED_PLUGIN, "profile_multi", "gate"),
        (PROFILE_GUIDED_PLUGIN, "profile_multi", "profile"),
    ],
)
def test_public_policy_trajectory_matches_golden(
    tmp_path: Path,
    plugin: OrchestrationPlugin,
    preset: str,
    scenario: str,
) -> None:
    async def run() -> dict[str, object]:
        script = _Script(*_scenario(scenario))
        rounds = 2 if scenario == "rollback" else 1
        official_every = 1 if scenario == "gate" else 10
        profile_guided = plugin is PROFILE_GUIDED_PLUGIN
        facts = RunFacts(
            domain_id="llm-serving",
            objective="Maximize token throughput.",
            profiler_id="torch" if scenario == "profile" else "none",
            accuracy_configured=scenario == "gate",
            benchmark_configured=scenario == "gate",
        )
        host = FakeRunHost(plugin, project_root=tmp_path, facts=facts, responder=script.respond)
        if profile_guided:
            for _round in range(rounds):
                host.commands.script(CommandResult(output=_attribution(), exit_code=0))
        if scenario == "gate":
            host.evaluation.script_accuracy(AccuracyEvaluation(executed=True))
            host.evaluation.script_benchmark(
                BenchmarkEvaluation(
                    executed=True,
                    metric_name="throughput",
                    metric_value=120.0,
                    metric_unit="tokens/s",
                    row={"throughput": 120.0},
                )
            )
        options = (
            _profile_options(rounds=rounds, official_every=official_every)
            if profile_guided
            else _plain_options(rounds=rounds, official_every=official_every)
        )
        try:
            assert await plugin.orchestrate(host, options) is RunStatus.SUCCEEDED
            state_model = plugin.state
            assert state_model is not None
            state = await host.state.load(state_model)
            assert state is not None
            assert plugin.project is not None
            return {
                "calls": script.calls,
                "artifacts": _artifact_snapshot(tmp_path),
                "projection": plugin.project(state).model_dump(mode="json"),
                "retained": host.workspaces.root.retained,
            }
        finally:
            await host.close()

    _assert_golden(preset, scenario, asyncio.run(run()), workspace=tmp_path)
