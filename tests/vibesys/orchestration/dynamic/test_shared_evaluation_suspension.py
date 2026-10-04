"""Joined dynamic continuations use requester rights over one physical capture."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Literal

import pytest
from tests.vibesys.orchestration.dynamic._profile_release_support import profile_release_effects
from tests.vibesys.orchestration.dynamic._support import Script, baseline_run

from vibesys.orchestration.dynamic.agents import IMPLEMENTER
from vibesys.orchestration.dynamic.lifecycle import (
    CancelEvaluation,
    CompleteIntent,
    EvaluationOutcome,
    IntentKind,
    IntentStage,
    LifecycleIntent,
    LifecycleState,
    RecoveryStarted,
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
from vibesys.orchestration.dynamic.transitions import (
    DeadlineReached,
    EvaluationObserved,
    EvaluationSettled,
)
from vibesys.run.dynamic_suspension import (
    EvaluationSuspension,
    EvaluationSuspensionInvariantError,
)
from vs_agent.api import (
    AgentClient,
    AgentExecutionPolicy,
    AgentSessionKey,
    AgentSessionSpec,
    AgentTurnRequest,
)
from vs_agent.api.testing import FakeAgentSessions, FakeDriver
from vs_evaluation.api import (
    EvaluationAgentRole,
    EvaluationState,
    EvaluationStepResult,
    EvidenceKind,
    StageState,
    SubmitCall,
    SubmittedReply,
)
from vs_runtime.api import AgentToolBindingContext
from vs_runtime.api.infrastructure import TrustedEvaluationPlan

if TYPE_CHECKING:
    from pathlib import Path

    from tests.vibesys.orchestration.dynamic._profile_release_support import ProfileReleaseEffects

    from vibesys.orchestration.dynamic.lifecycle import EvaluationContinuation
    from vs_runtime.api import AgentSession, CandidateWorkspace
    from vs_runtime.api.testing import FakeRun


@pytest.mark.asyncio
@pytest.mark.parametrize("reopened", [False, True], ids=["foreign-scope", "reopened-requester"])
@pytest.mark.parametrize("mode", ["settle", "cancel", "stale"])
async def test_workstream_b_joins_a_capture_and_resumes_once(
    tmp_path: Path, *, reopened: bool, mode: Literal["settle", "cancel", "stale"]
) -> None:
    """B's unchanged candidate shares A's physical work without owning A's wait."""
    base = baseline_run(tmp_path, Script({}))
    base.workspaces.set_default_patch("identical candidate content")
    root = await base.workspaces.root.snapshot("root")
    owner = await base.workspaces.create_candidate(root, member_id="a")
    requester = await base.workspaces.create_candidate(root, member_id="b")
    assert owner.id is not None
    assert requester.id is not None
    effects = profile_release_effects(
        tmp_path,
        base,
        plan=TrustedEvaluationPlan(
            accuracy_timeout_seconds=60,
            framework_setup_timeout_seconds=30,
        ),
    )
    calls: list[AgentTurnRequest] = []
    client = AgentClient(
        FakeDriver(
            answer={
                "summary": "Shared result checked.",
                "outcome": "continue",
                "next_step": "Done.",
            },
            on_turn=calls.append,
        )
    )
    try:
        handle = await _join(effects, owner, requester, reopened=reopened)
        session = await _session(base, client, tmp_path, requester)
        invocation = "b/implementer/1"
        state = DynamicState(
            workstreams=[
                DynamicWorkstream(
                    hypothesis_id="b",
                    sequence=1,
                    planning_call=1,
                    plan=WorkstreamPlan.model_validate(
                        {
                            "hypothesis_id": "b",
                            "title": "Shared capture",
                            "hypothesis": "Unchanged candidate",
                            "task": "Check shared result",
                            "pass_criteria": "Trusted capture settles",
                        }
                    ),
                    parent_revision=root,
                    phase=WorkstreamPhase.IMPLEMENTING,
                    budget=WorkstreamBudget(spent=1),
                )
            ],
            lifecycle=LifecycleState(
                intents={
                    invocation: LifecycleIntent(
                        operation_id=invocation,
                        scope_id="b",
                        generation=1,
                        kind=IntentKind.TURN,
                        invocation_id=invocation,
                        stage=IntentStage.DISPATCHED,
                    )
                }
            ),
        )

        async def commit(label: str) -> None:
            await effects.run.state.commit(state, label=label)

        shell = EvaluationSuspension(effects.run, state, asyncio.Lock(), commit)
        shell.cursors.record(
            invocation_id=invocation, workspace_id=requester.id, preceding_handles=()
        )
        await shell.yield_turn(
            0,
            requester,
            session,
            WaitingForEvaluation(
                kind="waiting_for_evaluation",
                handles=(handle,),
            ),
        )
        continuation = next(iter(state.lifecycle.continuations.values()))
        assert continuation.evaluation_scope_id == requester.id
        assert continuation.evaluation_generation == int(reopened)
        initial_calls = len(calls)
        budget = state.workstreams[0].budget
        await _drive(shell, effects, continuation, mode)
        if mode == "stale":
            await _join(effects, owner, requester, reopened=True)
            with pytest.raises(EvaluationSuspensionInvariantError, match="requester generation"):
                await shell.run_wait(0, requester, session)
            assert len(calls) == initial_calls
            assert state.workstreams[0].budget == budget
            await session.close()
            return
        reply, acknowledgement = await shell.run_wait(0, requester, session)
        assert isinstance(reply, ImplementerResult)
        assert len(calls) == initial_calls + 1
        assert calls[-1].expected_provider_session_id == session.checkpoint().provider_session_id
        assert state.workstreams[0].budget == budget
        await _assert_capture(effects, owner, state, calls[-1], cancel=mode == "cancel")
        await shell.apply(CompleteIntent(operation_id=acknowledgement))
        requests = await shell.apply(RecoveryStarted())
        assert not any(request.kind is IntentKind.RESUME for request in requests)
        assert len(calls) == initial_calls + 1
        await session.close()
    finally:
        client.close()
        await effects.close()


async def _join(
    effects: ProfileReleaseEffects,
    owner: CandidateWorkspace,
    requester: CandidateWorkspace,
    *,
    reopened: bool,
) -> str:
    assert owner.id is not None
    assert requester.id is not None
    for workspace in (owner, requester):
        effects.backend.bind(AgentToolBindingContext(IMPLEMENTER, workspace, workspace.id, str))
    if reopened:
        await effects.service.cancel_scope(requester.id)
        await effects.service.reopen_scope(requester.id)
    handles = []
    for workspace in (owner, requester):
        assert workspace.id is not None
        grant = effects.service.grant(
            principal_id=workspace.id,
            role=EvaluationAgentRole.IMPLEMENTER,
            scope_id=workspace.id,
        )
        submitted = await effects.service.dispatch(
            SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.ACCURACY,))
        )
        assert isinstance(submitted, SubmittedReply)
        handles.append(submitted.handle_id)
    assert handles[0] == handles[1]
    assert len(effects.executor.submissions) == 1
    return handles[1]


async def _session(
    base: FakeRun,
    client: AgentClient,
    tmp_path: Path,
    requester: CandidateWorkspace,
) -> AgentSession:
    key = AgentSessionKey.for_member(IMPLEMENTER.id, "b")
    spec = AgentSessionSpec(
        role=IMPLEMENTER.id,
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    client.run(session_spec=spec, turn=AgentTurnRequest(message="initial"), session_key=key)
    transport = FakeAgentSessions(client)
    transport.bind(key, spec, AgentTurnRequest(message="resume"))
    base.agents.bind_session_transport(transport)
    return await base.agents.create_session(IMPLEMENTER, workspace=requester, member_id="b")


async def _assert_capture(
    effects: ProfileReleaseEffects,
    owner: CandidateWorkspace,
    state: DynamicState,
    last_call: AgentTurnRequest,
    *,
    cancel: bool,
) -> None:
    continuation = next(iter(state.lifecycle.continuations.values()))
    handle = continuation.dependencies[0].handle
    if cancel:
        assert await effects.backend.status(handle) is EvaluationState.QUEUED
        assert effects.executor.backend.active_count == 1
    else:
        report = await effects.backend.recorded_snapshot(handle)
        assert report.request.owner_scope == owner.id
        assert report.request.owner_generation == 0
        assert len(continuation.settlements) == 1
        assert "Terminal status: passed" in last_call.message


async def _drive(
    shell: EvaluationSuspension,
    effects: ProfileReleaseEffects,
    continuation: EvaluationContinuation,
    mode: Literal["settle", "cancel", "stale"],
) -> None:
    dependency = continuation.dependencies[0]
    identity = dependency.model_dump(exclude={"candidate_revision"})
    if mode == "cancel":
        await shell.apply(
            EvaluationObserved(
                continuation_id=continuation.continuation_id,
                at_s=0,
                observation_state="pending",
                **identity,
            )
        )
        requests = await shell.apply(
            DeadlineReached(
                continuation_id=continuation.continuation_id,
                at_s=continuation.deadline_at_s,
            )
        )
        assert any(isinstance(request, CancelEvaluation) for request in requests)
        return
    effects.executor.set_state(
        dependency.handle,
        EvaluationState.SUCCEEDED,
        stage_results=(EvaluationStepResult(name="accuracy", state=StageState.SUCCEEDED),),
    )
    await effects.backend.status(dependency.handle)
    if mode == "stale":
        await shell.apply(
            EvaluationSettled(
                continuation_id=continuation.continuation_id,
                at_s=0,
                outcome=EvaluationOutcome.SUCCEEDED,
                **identity,
            )
        )
