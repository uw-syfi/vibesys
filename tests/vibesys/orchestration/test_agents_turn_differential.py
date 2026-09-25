"""Differential test: ``ctx.agents.turn`` renders the same prompt text for
every ``multi`` (and, by role-catalog reuse, ``profile_multi``) role.

``vibesys.loops.multi.turns`` no longer renders prompts itself (phase 3b):
every turn goes through ``ctx.agents.turn(role, ...)`` with the roles
declared across the roles family modules multi uses. So the render call this test observes
and replays is the one ``vibesys.orchestration.agents`` issues, not one
``turns.py`` makes directly.

Method: run the real ``multi`` strategy's "profile" scenario end-to-end (a
scripted ``FakeAgentClient``, the same golden harness
``tests/vibesys/golden/test_multi_golden.py`` uses), recording every
``render_template(name, template_dir=..., **kwargs)`` call
``vibesys.orchestration.agents`` makes alongside its rendered text. That
scenario exercises five of the six declared multi roles: pre-round
decision, plan, one profiler kind, a fresh implementer turn, and judge. For
each, replay ``ctx.agents.turn`` on a fresh test context with the matching
declared ``Role`` and the same captured context kwargs, and assert its
rendered system prompt is byte-identical to what the real run actually sent
the fake agent.

The sixth role, the implementer's continuation prompt, has no branch in the
"profile" scenario (it only ever runs a fresh implementer turn), so it is
covered directly: two independent ``ctx.agents.turn`` calls with the same
role and context must render identically.

``profile_multi`` reuses multi's exact role catalog (same templates; see
``vibesys.loops.profile_multi.turns``), so proving multi's roles here also
covers profile_multi's turns.
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
from vibesys.profilers import ProfilerKind
from vibesys.prompts.renderer import render_template as _real_render_template
from vibesys.roles.common import Verdict
from vibesys.roles.designer import MULTI_ORCHESTRATOR_PLAN
from vibesys.roles.implementer import (
    MULTI_IMPLEMENTER,
    MULTI_IMPLEMENTER_CONTINUATION,
    ImplementerResponse,
)
from vibesys.roles.judge import MULTI_JUDGE, JudgeResponse
from vibesys.roles.pre_round import MULTI_PRE_ROUND_DECISION, PreRoundDecision
from vibesys.roles.profiler import MULTI_PROFILERS, ProfilerSummary
from vibesys.schemas import OrchestratorPlan
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext
    from vibesys.runtime import Role


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


def _replay(tmp_path: Path, role: Role, context: dict[str, object], label: str) -> str:
    """Call ``ctx.agents.turn`` on a fresh context and return its system prompt."""
    replay_runner = FakeAgentClient(backend_name="stub")
    replay_runner.enqueue("testrole", role.fallback())

    async def body(ctx: RunContext) -> str:
        agent = await ctx.agents.spawn(ctx.agents.default_definition("testrole"))
        try:
            await ctx.agents.turn(role, agent=agent, context=context, label=label)
        finally:
            await agent.close()
        return replay_runner.calls_for("testrole")[0].system_prompt

    return run_with_context(tmp_path, replay_runner, body)


def test_every_multi_role_prompt_matches_ctx_agents_turn(tmp_path: Path) -> None:
    recorded: list[_RecordedRender] = []
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue(
        "orchestrator",
        PreRoundDecision(
            need_profile=True, profile_focus="prefill kernels", reasoning="scripted: profile"
        ),
        _plan(),
    )
    runner.enqueue(
        "profiler",
        ProfilerSummary(
            analysis="prefill dominates step time",
            bottlenecks="1. per-request prefill launch: 40% of step time",
            suggestions="batch prefill requests",
            perf_metric=1000.0,
            perf_unit="tok/s",
        ),
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
    with patch("vibesys.orchestration.agents.render_template", _record_render_calls(recorded)):
        real_run = run_scripted(
            tmp_path / "real",
            orchestration_id="multi-agent",
            descriptor=descriptor,
            orchestrator_factory=MultiAgentOrchestrator,
            runner=runner,
            profiler_kind=ProfilerKind.TORCH,
        )
    assert real_run.result is True

    def recorded_for(name: str) -> _RecordedRender:
        return next(call for call in recorded if call.name == name)

    real_orchestrator_calls = [c for c in runner.calls if c.kind == "orchestrator"]
    real_profiler_prompt = runner.calls_for("profiler")[0].system_prompt
    real_implementer_prompt = runner.calls_for("implementer")[0].system_prompt
    real_judge_prompt = runner.calls_for("judge")[0].system_prompt

    cases: tuple[tuple[Role, str, str, str], ...] = (
        (
            MULTI_PRE_ROUND_DECISION,
            "orchestrator_pre_round_prompt.j2",
            real_orchestrator_calls[0].system_prompt,
            "replay-pre-round",
        ),
        (
            MULTI_ORCHESTRATOR_PLAN,
            "orchestrator_plan_prompt.j2",
            real_orchestrator_calls[1].system_prompt,
            "replay-plan",
        ),
        (
            MULTI_PROFILERS[ProfilerKind.TORCH],
            "profilers/torch.j2",
            real_profiler_prompt,
            "replay-profiler",
        ),
        (MULTI_IMPLEMENTER, "implementer_prompt.j2", real_implementer_prompt, "replay-implementer"),
        (MULTI_JUDGE, "judge_prompt.j2", real_judge_prompt, "replay-judge"),
    )
    for index, (role, template_name, real_prompt, label) in enumerate(cases):
        call = recorded_for(template_name)
        assert call.text == real_prompt, f"recorded render for {template_name} mismatched"
        replayed = _replay(tmp_path / f"replay-{index}", role, call.kwargs, label)
        assert replayed == real_prompt, f"ctx.agents.turn mismatched the real prompt for {role.id}"


def test_implementer_continuation_role_renders_through_ctx_agents_turn(tmp_path: Path) -> None:
    """No branch in the scripted scenario above reaches the continuation
    prompt (it only ever runs a fresh implementer turn), so cover it by
    replaying the same role and context twice and comparing byte-for-byte.
    """
    context: dict[str, object] = {
        "reference_path": None,
        "modality": "text_generation",
        "interface": "inprocess",
        "domain_implementer": "",
        "task": "batch the decode step",
        "pass_criteria": "throughput improves",
        "objective": "Maximize tok/s throughput.",
        "objective_location": "OBJECTIVE.md",
        "plan_artifact_location": "progress-artifacts/plans/round-0001.json",
        "hypothesis_id": "H-01",
        "hypothesis": "batching the decode step removes per-token launch overhead",
        "activation_evidence": "",
        "falsification_criteria": "",
        "expected_effect": "",
        "minimum_acceptance_criteria": "",
        "invariants": "",
        "progress_location": "PROGRESS.md",
        "pareto_archive_location": "progress-artifacts/pareto-frontier.md",
        "validation_location": "progress-artifacts/validation",
        "validation_recipe_contract_location": "progress-artifacts/validation/RECIPE_SCHEMA.json",
        "retry": 1,
        "feedback": None,
        "continuation_step": "finish wiring the decode batch path",
        "framework_revert_applied": False,
        "framework_revert_round": None,
        "framework_revert_commit": None,
        "gate_revalidation_pending": False,
        "gate_approved_perf_metric": None,
        "gate_approved_perf_unit": None,
        "gate_approved_evaluation_artifact": None,
        "runtime_notes": "",
        "profile_execution": "local",
        "framework_benchmark_enabled": False,
        "official_evaluation_due": False,
        "official_evaluation_reason": None,
        "recommended_skills": [],
        "prior_attempt_artifact_locations": (),
    }
    first = _replay(tmp_path / "first", MULTI_IMPLEMENTER_CONTINUATION, context, "replay-1")
    second = _replay(tmp_path / "second", MULTI_IMPLEMENTER_CONTINUATION, context, "replay-2")
    assert first == second
    assert "finish wiring the decode batch path" in first
