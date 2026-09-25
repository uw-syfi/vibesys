"""Differential test: ``ctx.agents.turn`` renders the identical prompt text
the ``multi`` strategy's ``turns.py`` wrappers render today, for the same
inputs. This is the contract phase 3b/3c rely on when they migrate
``loops/`` call sites onto ``ctx.agents.turn``.

Method: run the real ``multi`` strategy end-to-end against a scripted
``FakeAgentClient`` (the golden harness), recording every
``render_template(name, template_dir=..., **kwargs)`` call `turns.py` makes
alongside the resulting recorded prompt. Then call ``ctx.agents.turn`` with
the matching declared ``Role`` and the exact same context kwargs, on a
freshly opened test context, and assert its rendered system prompt is
byte-identical to what the real run actually sent the fake agent.

Scope note (see the phase-3a report): covers multi's pre-round-decision and
orchestrator-plan roles (both ``ReadOnly``, ``Fresh``-session, and rendered
through plain ``render_template`` -- the case ``ctx.agents.turn`` supports
today). Multi's profiler/implementer/judge, single, and issue_queue are not
covered here: issue_queue renders through the backend-fragment-aware
``Prompt`` class, which ``ctx.agents.turn`` does not yet reproduce (see
``vibesys/roles/issue_queue.py``); extending this same pattern to the
remaining multi/single roles is mechanical and deferred to keep this phase's
scope bounded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from unittest.mock import patch

from tests.vibesys.golden.harness import run_scripted
from tests.vibesys.orchestration.harness import run_with_context

from vibesys.agent_run.options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.evaluators.metrics import MetricSpace
from vibesys.loops.multi.orchestration import MultiAgentOrchestrator
from vibesys.prompts.renderer import render_template as _real_render_template
from vibesys.roles.multi import MULTI_ORCHESTRATOR_PLAN, MULTI_PRE_ROUND_DECISION
from vibesys.schemas import (
    ImplementerResponse,
    JudgeResponse,
    OrchestratorPlan,
    PreRoundDecision,
    Verdict,
)
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext


def _options() -> AgentOrchestrationOptions:
    return AgentOrchestrationOptions.model_validate(
        {
            "interface": "inprocess",
            "max_rounds": 1,
            "max_retries_per_round": 1,
            "judge_every": 1,
            "official_eval_every": 1,
            "memory_layout": "files",
            "metric_space": MetricSpace(),
        }
    )


def _plan() -> OrchestratorPlan:
    return OrchestratorPlan(
        hypothesis_id="H-01",
        hypothesis="batching the prefill step removes per-request launch overhead",
        task="batch the prefill step",
        pass_criteria="throughput improves without regressing accuracy",  # noqa: S106  # tracked: #288
        reasoning="scripted differential fixture",
    )


@dataclass(slots=True)
class _RecordedRender:
    name: str
    kwargs: dict[str, object] = field(default_factory=dict)
    text: str = ""


def _record_render_calls(recorded: list[_RecordedRender]):  # noqa: ANN202  # tracked: #288
    def _wrapper(name: str, *, template_dir=None, **kwargs: object) -> str:  # noqa: ANN001  # tracked: #288
        text = _real_render_template(name, template_dir=template_dir, **kwargs)
        recorded.append(_RecordedRender(name=name, kwargs=kwargs, text=text))
        return text

    return _wrapper


def test_pre_round_decision_and_plan_prompts_match_multi_strategy(tmp_path: Path) -> None:
    recorded: list[_RecordedRender] = []
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator",
        PreRoundDecision(
            need_profile=False, profile_focus="", reasoning="scripted: skip profiling"
        ),
        _plan(),
    )
    runner.enqueue(
        "implementer",
        ImplementerResponse(
            summary="batched the prefill step",
            expected_behavior="higher steady-state throughput",
            evidence="ran the local checks",
        ),
    )
    runner.enqueue(
        "judge",
        JudgeResponse(
            analysis="reviewed the diff and the checks", feedback="", verdict=Verdict.PASS
        ),
    )

    descriptor = descriptor_from_options(_options(), orchestration_id="multi-agent")
    with patch("vibesys.loops.multi.turns.render_template", _record_render_calls(recorded)):
        real_run = run_scripted(
            tmp_path / "real",
            orchestration_id="multi-agent",
            descriptor=descriptor,
            orchestrator_factory=MultiAgentOrchestrator,
            runner=runner,
        )
    assert real_run.result is True

    pre_round_call = next(c for c in recorded if c.name == "orchestrator_pre_round_prompt.j2")
    plan_call = next(c for c in recorded if c.name == "orchestrator_plan_prompt.j2")

    real_calls = [c for c in runner.calls if c.kind == "orchestrator"]
    real_pre_round_prompt = real_calls[0].system_prompt
    real_plan_prompt = real_calls[1].system_prompt
    assert pre_round_call.text == real_pre_round_prompt
    assert plan_call.text == real_plan_prompt

    replay_runner = FakeAgentClient(backend_name="stub")
    replay_runner.enqueue(
        "testrole", PreRoundDecision(need_profile=False, profile_focus="", reasoning="x")
    )

    async def body(ctx: RunContext) -> str:
        agent = await ctx.agents.spawn(ctx.agents.default_definition("testrole"))
        try:
            await ctx.agents.turn(
                MULTI_PRE_ROUND_DECISION,
                agent=agent,
                context=pre_round_call.kwargs,
                label="replay-pre-round",
            )
        finally:
            await agent.close()
        return replay_runner.calls_for("testrole")[0].system_prompt

    rendered_pre_round = run_with_context(tmp_path / "replay1", replay_runner, body)
    assert rendered_pre_round == real_pre_round_prompt

    replay_runner_2 = FakeAgentClient(backend_name="stub")
    replay_runner_2.enqueue("testrole", _plan())

    async def body2(ctx: RunContext) -> str:
        agent = await ctx.agents.spawn(ctx.agents.default_definition("testrole"))
        try:
            await ctx.agents.turn(
                MULTI_ORCHESTRATOR_PLAN,
                agent=agent,
                context=plan_call.kwargs,
                label="replay-plan",
            )
        finally:
            await agent.close()
        return replay_runner_2.calls_for("testrole")[0].system_prompt

    rendered_plan = run_with_context(tmp_path / "replay2", replay_runner_2, body2)
    assert rendered_plan == real_plan_prompt
