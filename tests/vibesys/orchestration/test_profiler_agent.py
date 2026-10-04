"""Public contract tests for runtime-backed profiler conversations."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.run.evaluation_backend import SemanticEvaluationBackend, SemanticEvaluationIdentity
from vibesys.run.profiler_agent import ProfilerEvaluationAccess, RuntimeProfilerTurnProvision
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
    EvaluationAgentRole,
    EvaluationAgentService,
    EvaluationDependencyError,
    EvaluationState,
    EvidenceKind,
    ProfilerAgentResult,
    ProfilerResultOutcome,
    SubmitCall,
    SubmittedReply,
)
from vs_evaluation.api.testing import FakeClock, FakeEvaluationExecutor, InMemoryEvaluationNamespace
from vs_runtime.api import AgentCapability, AgentRole, AgentToolBindingContext, CandidateWorkspace
from vs_runtime.api.testing import (
    FakeEvaluation,
    FakeWorkspace,
    FakeWorkspaceAgentSessions,
    FakeWorkspaces,
    TurnResponder,
)

if TYPE_CHECKING:
    from pydantic import BaseModel


def _runtime(
    role: AgentRole,
    *,
    responder: TurnResponder,
) -> tuple[FakeWorkspaceAgentSessions, FakeWorkspaces]:
    agents = FakeWorkspaceAgentSessions(
        (role,),
        responder=responder,
        supported_agent_capabilities={
            AgentCapability.PROVIDER_SESSION_RESUME,
            AgentCapability.DURABLE_TURN_CONTINUATION,
        },
    )
    workspaces = FakeWorkspaces(
        FakeWorkspace(
            path=Path("/project"),
            revision="snapshot-a",
            known_revisions={"snapshot-b"},
        ),
        supports_parallel_candidates=True,
        sessions=agents,
    )
    workspaces.set_default_patch("same profiler candidate")
    return agents, workspaces


@pytest.mark.asyncio
async def test_runtime_profiler_reuses_conversation_on_requested_snapshots() -> None:
    role = AgentRole(id="profiler", system_prompt="Investigate performance.")
    response = ProfilerAgentResult(
        outcome=ProfilerResultOutcome.OBSERVED,
        narrative="The kernel launch path dominates.",
        evidence_ids=("a" * 64,),
    )

    def respond(
        _role: AgentRole,
        _history: tuple[str, ...],
        _message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        return response.model_dump()

    agents, workspaces = _runtime(role, responder=respond)
    provision = RuntimeProfilerTurnProvision(role, agents, workspaces)

    first = await provision.run_turn(
        session_id="conversation-1",
        operation_id="operation-1",
        request="Find the dominant launch overhead.",
        scope_id="candidate-1",
        candidate_snapshot_id="snapshot-a",
    )
    second = await provision.run_turn(
        session_id="conversation-1",
        operation_id="operation-2",
        request="Check whether batching removes it.",
        scope_id="candidate-1",
        candidate_snapshot_id="snapshot-b",
    )

    assert first == response
    assert second == response
    assert provision.identity == RuntimeProfilerTurnProvision(role, agents, workspaces).identity
    assert len(agents.sessions) == 1
    assert agents.sessions[0].member_id == "conversation-1"
    assert "Candidate snapshot: `snapshot-a`" in agents.sessions[0].history[0]
    assert "Request: Find the dominant launch overhead." in agents.sessions[0].history[0]
    assert workspaces.candidates[0].restore_calls[-1] == ("snapshot-b", True)

    await provision.cancel("completed-operation")
    await provision.cancel_scope("candidate-1")

    assert agents.sessions[0].closed
    assert workspaces.candidates[0].discarded
    await provision.close()


@pytest.mark.asyncio
async def test_runtime_profiler_discards_workspace_when_session_creation_fails() -> None:
    role = AgentRole(id="profiler", system_prompt="Investigate performance.")
    agents, workspaces = _runtime(role, responder=lambda *_args: {})
    agents.script_creation(RuntimeError("agent unavailable"))
    provision = RuntimeProfilerTurnProvision(role, agents, workspaces)

    with pytest.raises(RuntimeError, match="agent unavailable"):
        await provision.run_turn(
            session_id="conversation-1",
            operation_id="operation-1",
            request="Find the bottleneck.",
            scope_id=None,
            candidate_snapshot_id="snapshot-a",
        )

    assert workspaces.candidates[0].discarded
    assert agents.sessions == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("shared", "reopened", "owner_reopened"),
    [
        (False, False, False),
        (False, True, False),
        (True, False, False),
        (True, True, False),
        (True, False, True),
        (True, True, True),
    ],
)
async def test_profiler_yields_pending_evaluation_and_resumes_once(
    tmp_path: Path, *, shared: bool, reopened: bool, owner_reopened: bool
) -> None:
    """A pending capture spends host waits, with no profiler polling turns."""
    role = AgentRole(
        id="profiler",
        system_prompt="Investigate performance.",
        required_capabilities=frozenset(
            {AgentCapability.PROVIDER_SESSION_RESUME, AgentCapability.DURABLE_TURN_CONTINUATION}
        ),
    )
    response = ProfilerAgentResult(
        outcome=ProfilerResultOutcome.UNSUPPORTED,
        narrative="The capture failed.",
        unsupported_reason="The requested profile did not complete.",
    )
    handles: list[str] = []
    turns: list[str] = []

    async def respond(
        _role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> object:
        turns.append(message)
        handle = await _submit_profile(
            role, workspaces.candidates[-1], backend, service, reopened=reopened
        )
        handles.append(handle)
        return {"kind": "waiting_for_evaluation", "handles": handles}

    agents, workspaces = _runtime(role, responder=respond)
    namespace = InMemoryEvaluationNamespace()
    executor = _OwnedFakeExecutor(
        clock=FakeClock(),
        supported_evidence_kinds=(EvidenceKind.PROFILE.value,),
        advance_clock_on_timeout=False,
    )
    digest = ContentDigest.sha256(b"profile identity")
    backend = SemanticEvaluationBackend(
        FakeEvaluation(),
        workspaces,
        namespace,
        SemanticEvaluationIdentity(evaluator=digest, workload=digest, environment=digest),
        executor=executor,
    )
    service = EvaluationAgentService(backend, namespace, tmp_path / "profile.sock")
    owner_handle = (
        await _submit_shared_profile(role, workspaces, backend, service, reopened=owner_reopened)
        if shared
        else None
    )
    client, calls = _continuation_transport(agents, tmp_path, role, response)
    provision = RuntimeProfilerTurnProvision(
        role,
        agents,
        workspaces,
        evaluation=ProfilerEvaluationAccess(
            backend=backend,
            settlements=service.settlements(),
            requester_generation=service.requester_generation,
            validate_wait=service.validate_wait,
            cancel_associations=lambda scope: _cancel_associations(backend, service, scope),
        ),
    )
    initial_calls = len(calls)
    operation = asyncio.create_task(
        provision.run_turn(
            session_id="conversation-1",
            operation_id="profile-operation",
            request="Find the dominant overhead.",
            scope_id="planned-profile",
            candidate_snapshot_id="snapshot-a",
        )
    )
    try:
        waiting = asyncio.create_task(executor.wait_started.wait())
        done, _ = await asyncio.wait({waiting, operation}, return_when=asyncio.FIRST_COMPLETED)
        if operation in done:
            await operation
        await waiting
        assert len(turns) == 1
        assert len(calls) == initial_calls
        executor.clock.advance(420)
        assert len(calls) == initial_calls
        (handle,) = handles
        if shared:
            assert handle == owner_handle
        executor.set_state(handle, EvaluationState.FAILED, failure="capture failed")
        assert await operation == response
        assert len(turns) == 1
        assert len(calls) == initial_calls + 1
        assert "capture failed" in calls[-1].message
        assert (
            calls[-1].expected_provider_session_id
            == agents.sessions[-1].checkpoint().provider_session_id
        )
        assert len(executor.submissions) == 1
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await provision.close()
        await backend.close()
        client.close()


async def _cancel_associations(
    backend: SemanticEvaluationBackend, service: EvaluationAgentService, scope: str
) -> None:
    await backend.drain_submissions(scope)
    for handle in await service.scope_handles(scope):
        await service.cancel_association(handle, scope)


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_path", ["cancel", "cancel_scope", "close"])
@pytest.mark.parametrize("shared", [False, True])
async def test_profiler_exit_withdraws_only_its_requester_association(
    tmp_path: Path, exit_path: str, *, shared: bool
) -> None:
    role = AgentRole(id="profiler", system_prompt="Investigate performance.")
    response = ProfilerAgentResult(
        outcome=ProfilerResultOutcome.UNSUPPORTED,
        narrative="The capture is incomplete.",
        unsupported_reason="The capture was queued.",
    )
    submitted = asyncio.Event()
    handles: list[str] = []

    async def respond(*_args: object) -> object:
        handles.append(
            await _submit_profile(role, workspaces.candidates[-1], backend, service, reopened=False)
        )
        submitted.set()
        if exit_path == "cancel":
            await asyncio.Event().wait()
        return response.model_dump()

    agents, workspaces = _runtime(role, responder=respond)
    namespace = InMemoryEvaluationNamespace()
    executor = _OwnedFakeExecutor(
        clock=FakeClock(), supported_evidence_kinds=(EvidenceKind.PROFILE.value,)
    )
    digest = ContentDigest.sha256(b"profile identity")
    backend = SemanticEvaluationBackend(
        FakeEvaluation(),
        workspaces,
        namespace,
        SemanticEvaluationIdentity(evaluator=digest, workload=digest, environment=digest),
        executor=executor,
    )
    service = EvaluationAgentService(backend, namespace, tmp_path / "cleanup.sock")
    owner = await workspaces.create_candidate("snapshot-a", member_id="owner")
    assert owner.id is not None
    owner_handle = (
        await _submit_profile(role, owner, backend, service, reopened=False) if shared else None
    )
    provision = RuntimeProfilerTurnProvision(
        role,
        agents,
        workspaces,
        evaluation=ProfilerEvaluationAccess(
            backend=backend,
            settlements=service.settlements(),
            requester_generation=service.requester_generation,
            validate_wait=service.validate_wait,
            cancel_associations=lambda scope: _cancel_associations(backend, service, scope),
        ),
    )
    operation = asyncio.create_task(
        provision.run_turn(
            session_id="conversation-1",
            operation_id="operation-1",
            request="Find the bottleneck.",
            scope_id=owner.id,
            candidate_snapshot_id="snapshot-a",
        )
    )
    try:
        await submitted.wait()
        child = workspaces.candidates[-1]
        assert child.id is not None
        (handle,) = handles
        if exit_path == "cancel":
            await provision.cancel("operation-1")
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            assert await operation == response
            if exit_path == "cancel_scope":
                await provision.cancel_scope(owner.id)
            else:
                await provision.close()
        assert child.discarded
        with pytest.raises(EvaluationDependencyError):
            await service.association_generation(handle, scope_id=child.id)
        if shared:
            assert handle == owner_handle
            assert await service.association_generation(handle, scope_id=owner.id) == 0
            assert executor.cancellations == []
            await service.cancel_scope(owner.id)
        assert executor.cancellations == [handle]
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        await provision.close()
        await backend.close()


async def _submit_shared_profile(
    role: AgentRole,
    workspaces: FakeWorkspaces,
    backend: SemanticEvaluationBackend,
    service: EvaluationAgentService,
    *,
    reopened: bool,
) -> str:
    owner = await workspaces.create_candidate("snapshot-a", member_id="owner")
    return await _submit_profile(role, owner, backend, service, reopened=reopened)


async def _submit_profile(
    role: AgentRole,
    candidate: CandidateWorkspace,
    backend: SemanticEvaluationBackend,
    service: EvaluationAgentService,
    *,
    reopened: bool,
) -> str:
    assert candidate.id is not None
    if reopened:
        await service.cancel_scope(candidate.id)
        await service.reopen_scope(candidate.id)
    backend.bind(AgentToolBindingContext(role, candidate, "profiler", str))
    grant = service.grant(
        principal_id="profiler:conversation-1",
        role=EvaluationAgentRole.PROFILER,
        scope_id=candidate.id,
    )
    submitted = await service.dispatch(
        SubmitCall(token=grant.token, evidence_kinds=(EvidenceKind.PROFILE,))
    )
    assert isinstance(submitted, SubmittedReply)
    return submitted.handle_id


class _OwnedFakeExecutor(FakeEvaluationExecutor):
    """The external executor Fake with the run-owned close contract."""

    async def close(self) -> None:
        """The in-memory executor owns no external resources."""


def _continuation_transport(
    agents: FakeWorkspaceAgentSessions,
    path: Path,
    role: AgentRole,
    response: ProfilerAgentResult,
) -> tuple[AgentClient, list[AgentTurnRequest]]:
    """Bind the public provider Fake to the runtime Fake's same session journal."""
    calls: list[AgentTurnRequest] = []
    client = AgentClient(FakeDriver(answer=response.model_dump(), on_turn=calls.append))
    key = AgentSessionKey(SessionScope.MEMBER, "profiler:conversation-1")
    spec = AgentSessionSpec(
        role=role.id,
        provider="fake",
        workspace=path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    client.run(session_spec=spec, turn=AgentTurnRequest(message="initial"), session_key=key)
    transport = FakeAgentSessions(client)
    transport.bind(key, spec, AgentTurnRequest(message="resume"))
    agents.bind_session_transport(transport)
    return client, calls
