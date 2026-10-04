"""Public contract tests for runtime-backed profiler conversations."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.orchestration.profiler_agent import RuntimeProfilerTurnProvision
from vs_evaluation.api import (
    ProfilerAgentResult,
    ProfilerResultOutcome,
)
from vs_runtime.api import AgentCapability, AgentRole
from vs_runtime.api.testing import (
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
        supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
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
