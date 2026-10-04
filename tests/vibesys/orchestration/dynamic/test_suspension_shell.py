"""Composed evaluation suspension over public runtime and owning-library Fakes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic._support import (
    Script,
    baseline_run,
)

from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vibesys.orchestration.dynamic.lifecycle import (
    IntentKind,
    IntentStage,
    LifecycleIntent,
    LifecycleState,
)
from vibesys.orchestration.dynamic.models import (
    DynamicState,
    DynamicWorkstream,
    ImplementerResult,
    WaitingForEvaluation,
    WorkstreamBudget,
    WorkstreamPhase,
    WorkstreamPlan,
)
from vibesys.orchestration.dynamic.suspension import EvaluationSuspension
from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
    SessionScope,
)
from vs_agent.api.testing import FakeAgentSessions, FakeDriver
from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvidenceFingerprints,
    StageState,
)
from vs_evaluation.api.testing import FakeEvaluationSettlements

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("elapsed_s", [240, 301, 420])
def test_host_wait_spends_no_agent_calls_or_attempts(elapsed_s: int, tmp_path: Path) -> None:
    """A multi-minute wait crosses the coordinator bound without agent polling."""

    async def scenario() -> None:
        run = baseline_run(tmp_path, Script({}))
        root = await run.workspaces.root.snapshot("root")
        workspace = await run.workspaces.create_candidate(root, member_id="held")
        calls: list[AgentTurnRequest] = []
        client = AgentClient(
            FakeDriver(
                answer={
                    "summary": "Trusted result checked.",
                    "outcome": "continue",
                    "next_step": "Done.",
                },
                on_turn=calls.append,
            )
        )
        key = AgentSessionKey(SessionScope.MEMBER, f"{IMPLEMENTER.id}:held")
        spec = AgentSessionSpec(
            role=IMPLEMENTER.id,
            provider="fake",
            workspace=tmp_path,
            policy=AgentExecutionPolicy(require_enforcement=False),
        )
        client.run(session_spec=spec, turn=AgentTurnRequest(message="initial"), session_key=key)
        transport = FakeAgentSessions(client)
        transport.bind(key, spec, AgentTurnRequest(message="resume"))
        run.agents.bind_session_transport(transport)
        session = await run.agents.create_session(
            IMPLEMENTER, workspace=workspace, member_id="held"
        )
        settlements = FakeEvaluationSettlements()
        digest = ContentDigest.sha256(b"immutable capture")
        handle = await settlements.submit(
            EvaluationRequest(
                key="held",
                owner_scope=workspace.id,
                owner_generation=0,
                stages=(EvaluationStep(name="benchmark", payload={}),),
            ),
            EvidenceFingerprints(
                candidate=digest, evaluator=digest, workload=digest, environment=digest
            ),
        )
        run.evaluation.settlement_observations = settlements
        run.evaluation.submitted_revisions[handle] = root
        run.evaluation.submitted_generations[handle] = 0
        run.evaluation.accepted_evidence[handle] = ("a" * 64,)
        invocation = "held/implementer/1"
        plan = WorkstreamPlan.model_validate(
            {
                "hypothesis_id": "held",
                "title": "Held evaluation",
                "hypothesis": "Held candidate improves.",
                "task": "Evaluate the candidate.",
                "pass_criteria": "Trusted measurement completes.",
            }
        )
        state = DynamicState(
            workstreams=[
                DynamicWorkstream(
                    hypothesis_id="held",
                    sequence=1,
                    planning_call=1,
                    plan=plan,
                    parent_revision=root,
                    phase=WorkstreamPhase.IMPLEMENTING,
                    budget=WorkstreamBudget(spent=1),
                )
            ],
            lifecycle=LifecycleState(
                intents={
                    invocation: LifecycleIntent(
                        operation_id=invocation,
                        scope_id="held",
                        generation=1,
                        kind=IntentKind.TURN,
                        invocation_id=invocation,
                        stage=IntentStage.DISPATCHED,
                    )
                }
            ),
        )

        async def commit(_label: str) -> None:
            await run.state.commit(state)

        shell = EvaluationSuspension(run, state, asyncio.Lock(), commit)
        await shell.yield_turn(
            0,
            workspace,
            session,
            WaitingForEvaluation(kind="waiting_for_evaluation", handles=(handle,)),
        )
        budget = state.workstreams[0].budget
        initial_calls = len(calls)
        settlements.executor.wait_started.clear()
        task = asyncio.create_task(shell.run_wait(0, workspace, session))
        await settlements.executor.wait_started.wait()
        settlements.executor.clock.advance(elapsed_s)
        assert len(calls) == initial_calls
        assert state.workstreams[0].budget == budget
        settlements.executor.set_state(
            handle,
            EvaluationState.SUCCEEDED,
            stage_results=(EvaluationStepResult(name="benchmark", state=StageState.SUCCEEDED),),
        )
        await settlements.coordinator.status(handle)
        report = await settlements.coordinator.recorded_snapshot(handle)
        run.evaluation.submitted_reports[handle] = report.model_dump_json()
        reply, _ = await task
        assert isinstance(reply, ImplementerResult)
        assert reply.summary == "Trusted result checked."
        assert len(calls) == initial_calls + 1
        assert calls[-1].expected_provider_session_id == session.checkpoint().provider_session_id
        assert state.workstreams[0].budget == budget
        await session.close()
        await workspace.discard()
        client.close()

    asyncio.run(scenario())
