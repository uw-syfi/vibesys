"""Structured corrections cross the real runtime and durable agent boundary."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

import pytest
from pydantic import BaseModel, RootModel
from tests.support.runtime_agent_sessions import (
    _ClientFactory,
    _durable_session_slot,
    _environment,
    _EnvironmentOpener,
    _runtime,
    _RuntimeEffects,
)

from vibesys.hypothesis import OrchestratorPlan
from vibesys.orchestration.dynamic import agents as dynamic
from vibesys.orchestration.dynamic.models import (
    ImplementerReply,
    JudgeReply,
    PortfolioPlan,
    WaitingForEvaluation,
)
from vibesys.orchestration.evolve import agents as evolve
from vibesys.orchestration.evolve.models import (
    JudgeResponse as EvolveJudgeResponse,
)
from vibesys.orchestration.evolve.models import (
    MutatorResponse,
)
from vibesys.orchestration.issue_queue import agents as issue_queue
from vibesys.orchestration.issue_queue.models import (
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    PerfMetrics,
)
from vibesys.orchestration.multi import agents as multi
from vibesys.orchestration.multi.contracts import (
    ImplementerResponse,
    JudgeResponse,
    PreRoundDecision,
)
from vibesys.orchestration.profilers import ProfilerSummary
from vibesys.orchestration.single import agents as single
from vibesys.orchestration.single.models import SingleAgentRoundResponse
from vibesys.orchestration.structured_turn import structured_turn
from vs_agent.api import (
    AgentClient,
    AgentOutputSchemaError,
    DurableSessionStore,
    StdioServerDescriptor,
)
from vs_agent.api.testing import FakeAgentInvocationStore, FakeDriver, FakeTurnScript
from vs_evaluation.api import ProfilerAgentResult
from vs_runtime.api import (
    AgentCapability,
    SessionResumeError,
    StructuredResponseError,
    WorkspaceAccess,
)
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink

if TYPE_CHECKING:
    from pathlib import Path

    from vs_agent.api import AgentTurnRequest
    from vs_runtime.api import AgentRole


VERIFIED_CRITERIA = "Verified behavior"

CASES = (
    pytest.param(
        dynamic.IMPLEMENTER,
        RootModel[ImplementerReply].model_validate(
            {"summary": "Preserved implementation", "outcome": "blocked", "next_step": "Measure"}
        ),
        id="dynamic-implementer",
    ),
    pytest.param(
        dynamic.JUDGE,
        RootModel[JudgeReply].model_validate({"passed": True, "analysis": "Preserved review"}),
        id="dynamic-judge",
    ),
    pytest.param(
        dynamic.PROFILER,
        ProfilerAgentResult(
            outcome="observed", narrative="Preserved profile", evidence_ids=("a" * 64,)
        ),
        id="dynamic-profiler",
    ),
    pytest.param(
        dynamic.PROFILER,
        RootModel[ProfilerAgentResult | WaitingForEvaluation](
            ProfilerAgentResult(
                outcome="unsupported",
                narrative="Measurement unavailable",
                unsupported_reason="No trusted capture",
            )
        ),
        id="dynamic-profiler-observed-to-unsupported",
    ),
    pytest.param(
        dynamic.ORCHESTRATOR,
        PortfolioPlan(
            reasoning="Preserved planning",
            workstreams=(
                {
                    "hypothesis_id": "candidate",
                    "title": "Measure candidate",
                    "hypothesis": "Candidate improves throughput",
                    "task": "Implement candidate",
                    "pass_criteria": "Trusted measurement passes",
                },
            ),
        ),
        id="dynamic-planner",
    ),
    pytest.param(
        multi.DESIGNER,
        OrchestratorPlan(task="Implement", pass_criteria=VERIFIED_CRITERIA, reasoning="Plan"),
        id="multi-planner",
    ),
    pytest.param(
        multi.DESIGNER,
        PreRoundDecision(need_profile=True, reasoning="Measure"),
        id="multi-profile-decision",
    ),
    pytest.param(
        multi.IMPLEMENTER,
        ImplementerResponse(summary="Implemented", expected_behavior="Faster"),
        id="multi-implementer",
    ),
    pytest.param(
        multi.JUDGE,
        JudgeResponse(analysis="Reviewed", feedback="", verdict="pass"),
        id="multi-judge",
    ),
    pytest.param(
        multi.PROFILER,
        ProfilerSummary(analysis="Measured", bottlenecks="CPU", suggestions="Batch"),
        id="multi-profiler",
    ),
    pytest.param(
        single.DESIGNER,
        OrchestratorPlan(task="Implement", pass_criteria=VERIFIED_CRITERIA, reasoning="Plan"),
        id="single-planner",
    ),
    pytest.param(
        single.IMPLEMENTER,
        SingleAgentRoundResponse(
            summary="Implemented",
            expected_behavior="Faster",
            self_review="Reviewed",
            feedback="",
            verdict="pass",
            bottlenecks="CPU",
            suggestions="Batch",
            profile_analysis="Measured",
        ),
        id="single-combined",
    ),
    pytest.param(
        evolve.MUTATOR,
        MutatorResponse(summary="Mutated", hypothesis="Faster", expected_behavior="Improves"),
        id="evolve-mutator",
    ),
    pytest.param(
        evolve.JUDGE,
        EvolveJudgeResponse(analysis="Reviewed", feedback="", verdict="pass"),
        id="evolve-judge",
    ),
    pytest.param(
        evolve.PROFILER,
        ProfilerSummary(analysis="Measured", bottlenecks="CPU", suggestions="Batch"),
        id="evolve-profiler",
    ),
    pytest.param(
        issue_queue.IMPLEMENTER,
        IssueImplementerResponse(issue_id=1, summary="Implemented", self_check="Passed"),
        id="issue-implementer",
    ),
    pytest.param(
        issue_queue.JUDGE,
        IssueJudgeResponse(issue_id=1, analysis="Reviewed", feedback="", verdict="pass"),
        id="issue-judge",
    ),
    pytest.param(
        issue_queue.PERF_EVALUATOR,
        IssuePerfEvalResponse(
            analysis="Measured",
            metrics=PerfMetrics(load_levels=()),
            evaluator_feedback=(),
            throughput_trend="improved",
            latency_trend="mixed",
        ),
        id="issue-performance",
    ),
)


@pytest.mark.parametrize(("role", "accepted"), CASES)
@pytest.mark.parametrize("provider_rejects", [False, True], ids=("invalid-json", "provider-schema"))
@pytest.mark.parametrize("correction_format", ["raw", "fenced", "invalid"])
def test_valid_correction_preserves_typed_role_reply_without_replay(
    tmp_path: Path,
    role: AgentRole,
    accepted: BaseModel,
    *,
    provider_rejects: bool,
    correction_format: Literal["raw", "fenced", "invalid"],
) -> None:
    slot = _durable_session_slot(tmp_path)
    ledger = FakeAgentInvocationStore()
    requests: list[AgentTurnRequest] = []
    invalid_json = (
        '{"outcome":"observed","narrative":"No trusted IDs"}' if role is dynamic.PROFILER else "{}"
    )
    initial = (
        AgentOutputSchemaError("initial schema rejected") if provider_rejects else invalid_json
    )

    correction = {
        "raw": accepted.model_dump_json(),
        "fenced": "Corrected reply:\n```json\n" + accepted.model_dump_json() + "\n```",
        "invalid": invalid_json,
    }[correction_format]
    durable = AgentCapability.DURABLE_TURN_CONTINUATION in role.required_capabilities

    async def scenario() -> None:
        for _ in range(2 if durable else 1):
            driver = FakeDriver(
                script=FakeTurnScript(answers=(initial, correction), reset_after_turn=2),
                on_turn=requests.append,
            )
            client = AgentClient(driver, session_store=DurableSessionStore(slot))
            runtime = _runtime(
                role,
                _RuntimeEffects(
                    _ClientFactory(client),
                    _EnvironmentOpener(_environment()),
                    FakeAgentExecutionLifecycleSink(),
                    tool_bindings={
                        tool.id: lambda _, name=tool.id: (StdioServerDescriptor(name, "fake"),)
                        for tool in role.extra_tools
                    },
                    invocation_store=lambda _: ledger,
                ),
            )
            try:
                session = await runtime.agents.create_session(
                    role,
                    workspace=runtime.workspaces.root,
                    member_id="member",
                    writable_paths=("report.md",)
                    if role.workspace_access is WorkspaceAccess.LIMITED
                    else (),
                )
                if provider_rejects and durable:
                    with pytest.raises(SessionResumeError) as failure:
                        await structured_turn(
                            session,
                            "Complete the role task",
                            type(accepted),
                            invocation_id="initial",
                        )
                    assert "checkpoint" in failure.value.detail
                    assert "unresolved" not in failure.value.detail
                elif correction_format == "invalid":
                    with pytest.raises(StructuredResponseError) as failure:
                        await structured_turn(
                            session,
                            "Complete the role task",
                            type(accepted),
                            invocation_id="initial" if durable else None,
                        )
                    assert type(accepted).__name__ in str(failure.value)
                    assert "unresolved" not in str(failure.value)
                else:
                    reply = await structured_turn(
                        session,
                        "Complete the role task",
                        type(accepted),
                        invocation_id="initial" if durable else None,
                    )
                    assert type(reply) is type(accepted)
                    assert reply == accepted
            finally:
                await runtime.agents.close()
                await runtime.workspaces.close()
        assert len(requests) == (1 if provider_rejects and durable else 2)
        if durable and not provider_rejects:
            assert requests[1].invocation_id == "initial/correction"
            assert requests[1].expected_provider_session_id is not None

    asyncio.run(scenario())
