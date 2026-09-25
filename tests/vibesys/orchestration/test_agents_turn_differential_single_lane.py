"""Differential test: every ``single``, ``profile_single`` and ``issue_queue``
role's turn, as invoked inside the real strategy, replays byte-identically
through a bare ``ctx.agents.turn`` call on a freshly opened context.

Method: run each real strategy end-to-end against a scripted
``FakeAgentClient`` (the same golden harness ``tests/vibesys/golden`` uses),
while wrapping ``_Agents.turn`` to record every call's ``(role, kwargs)``
(everything but the live ``agent`` handle, which is run-scoped and can't be
reused). For each recorded call, spawn a fresh agent on a bare
``RunContext`` (``tests.vibesys.orchestration.harness.run_with_context``)
and re-invoke ``ctx.agents.turn(role, agent=..., **kwargs)``; assert the
replayed system+user prompt matches what the real run actually sent the
fake agent for that same call.

Unlike ``test_agents_turn_differential.py`` (multi's phase-3a differential,
which diffs ``ctx.agents.turn`` against multi's still-hand-rolled
``turns.py``), single/profile_single/issue_queue's ``turns.py`` already
calls ``ctx.agents.turn`` directly as of this phase, so there is no separate
hand-rolled path left to diff against. This test instead pins that every
declared role -- including the per-call ``ReadOnly`` allow-list the
designer roles build with ``dataclasses.replace`` (single/profile_single)
and the ``backend``-driven fragment-aware rendering (issue_queue) -- renders
deterministically from its own ``(role, context, message)`` and nothing
else, so a future edit to a strategy's context-building can't silently
change a prompt without this test's replay catching it.

Kept in its own file (rather than extending ``test_agents_turn_differential.py``,
owned by the parallel multi/profile_multi migration) to avoid a merge
conflict between the two orchestration-simplify lanes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from tests.vibesys.golden.harness import run_scripted
from tests.vibesys.orchestration.harness import run_with_context

from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace
from vibesys.evaluators.perf_reply import (
    IssuePerfEvalResponse,
    PerfMetrics,
)
from vibesys.loops.agent_options import AgentOrchestrationOptions, descriptor_from_options
from vibesys.loops.issue_queue.entrypoint import IssueQueueOrchestrator
from vibesys.loops.issue_queue.orchestration import (
    IssueQueueOptions,
)
from vibesys.loops.issue_queue.orchestration import (
    descriptor_from_options as issue_queue_descriptor_from_options,
)
from vibesys.loops.single.orchestration import (
    ProfileGuidedSingleAgentOrchestrator,
    SingleAgentOrchestrator,
)
from vibesys.orchestration.agents import _Agents
from vibesys.roles.common import Verdict
from vibesys.roles.implementer import IssueImplementerResponse
from vibesys.roles.judge import IssueJudgeResponse
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.schemas import (
    CandidateDisposition,
    PerfTrend,
)
from vibesys.search.hypothesis import OrchestratorPlan
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vibesys.orchestration.runtime import RunContext
    from vibesys.runtime import Role
    from vs_agent.api.testing import FakeInvocation


@dataclass(slots=True)
class _RecordedTurn:
    role: Role
    kwargs: dict[str, Any] = field(default_factory=dict)


def _record_agents_turn_calls(recorded: list[_RecordedTurn]):  # noqa: ANN202  # tracked: #288
    """Wrap ``_Agents.turn`` to record every call's role and kwargs.

    ``agent`` is dropped (run-scoped, not replayable); every other keyword
    argument (``context``, ``message``, ``session_key``, ``label``,
    ``mcp_servers``, ``correction_message``, ``before_paid``, ``backend``)
    is exactly what a bare replay needs to reproduce the same render.
    """
    original = _Agents.turn

    async def _wrapper(
        self: _Agents,
        role: Role,
        *,
        agent: Any,  # noqa: ANN401  # tracked: #288
        **kwargs: Any,  # noqa: ANN401  # tracked: #288
    ) -> Any:  # noqa: ANN401  # tracked: #288
        recorded.append(_RecordedTurn(role=role, kwargs=dict(kwargs)))
        return await original(self, role, agent=agent, **kwargs)

    return _wrapper


def _assert_replay_matches_real(
    tmp_path: Path, recorded: _RecordedTurn, real_calls: Sequence[FakeInvocation]
) -> None:
    """Re-run one recorded ``ctx.agents.turn`` call on a bare context and
    assert its rendered system+user prompt matches what the real strategy
    run actually sent the fake agent for that same ``(role.id, label)``.
    """
    label = recorded.kwargs["label"]
    real_call = next(c for c in real_calls if c.kind == recorded.role.id and c.round_label == label)

    replay_runner = FakeAgentClient(
        backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True)
    )
    replay_runner.enqueue("testrole", recorded.role.fallback())

    async def body(ctx: RunContext) -> FakeInvocation:
        agent = await ctx.agents.spawn(ctx.agents.default_definition("testrole"))
        try:
            await ctx.agents.turn(recorded.role, agent=agent, **recorded.kwargs)
        finally:
            await agent.close()
        return replay_runner.calls_for("testrole")[0]

    replayed = run_with_context(tmp_path, replay_runner, body)
    assert replayed.system_prompt == real_call.system_prompt, (
        f"{recorded.role.id}/{label}: system prompt diverged"
    )
    assert replayed.user_prompt == real_call.user_prompt, (
        f"{recorded.role.id}/{label}: user prompt diverged"
    )


def _single_options() -> AgentOrchestrationOptions:
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


def _combined() -> SingleAgentRoundResponse:
    return SingleAgentRoundResponse(
        summary="batched the prefill step",
        expected_behavior="higher steady-state throughput",
        self_review="reviewed the diff and the checks",
        feedback="",
        verdict=Verdict.PASS,
        bottlenecks="prefill launch overhead dominates at low batch sizes",
        suggestions="batch decode requests next",
        profile_analysis="ran the local checks",
        candidate_disposition=CandidateDisposition.UNASSESSED,
    )


def test_single_roles_replay_identically(tmp_path: Path) -> None:
    recorded: list[_RecordedTurn] = []
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _plan())
    runner.enqueue("implementer", _combined())

    descriptor = descriptor_from_options(_single_options(), orchestration_id="single-agent")
    with patch.object(_Agents, "turn", _record_agents_turn_calls(recorded)):
        real_run = run_scripted(
            tmp_path / "real",
            orchestration_id="single-agent",
            descriptor=descriptor,
            orchestrator_factory=SingleAgentOrchestrator,
            runner=runner,
        )
    assert real_run.result is True
    assert {r.role.id for r in recorded} == {"orchestrator", "implementer"}

    for index, item in enumerate(recorded):
        _assert_replay_matches_real(tmp_path / f"replay-single-{index}", item, runner.calls)


def test_profile_single_roles_replay_identically(tmp_path: Path) -> None:
    recorded: list[_RecordedTurn] = []
    runner = FakeAgentClient(backend_name="stub")
    runner.enqueue("orchestrator", _plan())
    runner.enqueue("implementer", _combined())

    options = _single_options().model_copy(
        update={"profile_guided": ProfileGuidedInput(command=("profile-tool",))}
    )
    descriptor = descriptor_from_options(options, orchestration_id="profile-guided-single-agent")
    with (
        patch.object(_Agents, "turn", _record_agents_turn_calls(recorded)),
        patch(
            "vibesys.loops.single.session.run_attribution",
            side_effect=lambda *_a, **_k: (),
        ),
    ):
        real_run = run_scripted(
            tmp_path / "real",
            orchestration_id="profile-guided-single-agent",
            descriptor=descriptor,
            orchestrator_factory=ProfileGuidedSingleAgentOrchestrator,
            runner=runner,
        )
    assert real_run.result is True
    assert {r.role.id for r in recorded} == {"orchestrator", "implementer"}

    for index, item in enumerate(recorded):
        _assert_replay_matches_real(tmp_path / f"replay-profile-single-{index}", item, runner.calls)


def _issue_queue_options() -> IssueQueueOptions:
    return IssueQueueOptions.model_validate(
        {"max_rounds": 1, "max_attempts_per_issue": 3, "max_issues_per_perf_eval": 3}
    )


def _implementer(issue_id: int) -> IssueImplementerResponse:
    return IssueImplementerResponse(
        issue_id=issue_id,
        summary="Built the inference server.",
        files_touched=["server.py"],
        self_check="ran the accuracy checker locally",
    )


def _judge(issue_id: int) -> IssueJudgeResponse:
    return IssueJudgeResponse(
        issue_id=issue_id,
        analysis="reviewed the diff and the accuracy checks",
        feedback="",
        verdict=Verdict.PASS,
        new_issues_filed=[],
    )


def _perf_eval() -> IssuePerfEvalResponse:
    return IssuePerfEvalResponse(
        analysis="First benchmark run, no prior iteration to compare against.",
        metrics=PerfMetrics(load_levels=[]),
        evaluator_feedback=[],
        new_issue_ids=[],
        throughput_trend=PerfTrend.IMPROVED,
        latency_trend=PerfTrend.IMPROVED,
    )


def test_issue_queue_roles_replay_identically(tmp_path: Path) -> None:
    recorded: list[_RecordedTurn] = []
    runner = FakeAgentClient(backend_name="cli", capabilities=AgentCapabilities(mcp_servers=True))
    runner.enqueue("implementer", _implementer(1))
    runner.enqueue("judge", _judge(1))
    runner.enqueue("perf_eval", _perf_eval())

    descriptor = issue_queue_descriptor_from_options(_issue_queue_options())
    with patch.object(_Agents, "turn", _record_agents_turn_calls(recorded)):
        real_run = run_scripted(
            tmp_path / "real",
            orchestration_id="plain",
            descriptor=descriptor,
            orchestrator_factory=IssueQueueOrchestrator,
            runner=runner,
        )
    assert real_run.result is True
    assert {r.role.id for r in recorded} == {"implementer", "judge", "perf_eval"}

    for index, item in enumerate(recorded):
        _assert_replay_matches_real(tmp_path / f"replay-issue-queue-{index}", item, runner.calls)
