from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentTool,
    OrchestrationPlugin,
    RunHost,
    RunStatus,
    SessionClosedError,
    UnknownAgentRoleError,
    WorkspaceAccess,
    WorkspaceRef,
)
from vs_runtime.api.testing import FakeRunHost


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    rounds: int = 1


class _Reply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    answer: str


def _role(role_id: str = "implementer") -> AgentRole:
    return AgentRole(
        id=role_id,
        system_prompt="Improve the candidate.",
        tools=(AgentTool(id="shell"),),
        skills=("profiling",),
        workspace_access=WorkspaceAccess.READ_WRITE,
        required_capabilities=frozenset({AgentCapability.SESSION_REUSE}),
    )


async def _orchestrate(_host: RunHost, _options: BaseModel) -> RunStatus:
    return RunStatus.SUCCEEDED


def _plugin(*agents: AgentRole) -> OrchestrationPlugin:
    return OrchestrationPlugin(
        id="test-plugin",
        agents=agents,
        options=_Options,
        orchestrate=_orchestrate,
    )


def _workspace() -> WorkspaceRef:
    return WorkspaceRef(id="workspace-a", path=Path("/workspace-a"))


def test_role_is_a_strict_complete_declaration() -> None:
    role = _role()

    assert role.id == "implementer"
    assert role.system_prompt == "Improve the candidate."
    assert role.tools == (AgentTool(id="shell"),)
    with pytest.raises(ValidationError):
        AgentRole.model_validate(
            {
                "id": "implementer",
                "system_prompt": "prompt",
                "config_profile": "hidden alias",
            }
        )


def test_same_session_continues_and_second_creation_is_fresh() -> None:
    observed_history_lengths: list[int] = []

    def respond(
        _role: AgentRole,
        history: tuple[str, ...],
        message: str,
        response: type[BaseModel] | None,
    ) -> object:
        observed_history_lengths.append(len(history))
        return message if response is None else {"answer": message}

    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role), responder=respond)
        first = await host.agents.create_session(
            role, workspace=_workspace(), member_id="candidate-1"
        )
        assert await first.turn("one") == "one"
        assert await first.turn("two", response=_Reply) == _Reply(answer="two")
        second = await host.agents.create_session(role, workspace=_workspace())
        assert await second.turn("fresh") == "fresh"
        assert first.role == role
        assert first.workspace == _workspace()
        assert first.member_id == "candidate-1"
        await host.close()

    asyncio.run(scenario())
    assert observed_history_lengths == [0, 1, 0]


def test_factory_rejects_role_not_declared_by_plugin() -> None:
    async def scenario() -> None:
        host = FakeRunHost(_plugin(_role()))
        with pytest.raises(UnknownAgentRoleError, match="reviewer"):
            await host.agents.create_session(_role("reviewer"), workspace=_workspace())

    asyncio.run(scenario())


def test_run_owner_closes_sessions_and_closed_turn_fails() -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role))
        session = await host.agents.create_session(role, workspace=_workspace())
        await host.close()
        await host.close()
        assert session.closed
        with pytest.raises(SessionClosedError):
            await session.turn("late")

    asyncio.run(scenario())


def test_session_can_close_early_idempotently() -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role))
        session = await host.agents.create_session(role, workspace=_workspace())
        await session.close()
        await session.close()
        assert session.closed
        await host.close()

    asyncio.run(scenario())


def test_plugin_agents_tuple_is_unique_source_of_truth() -> None:
    role = _role()
    plugin = _plugin(role)

    assert plugin.id == "test-plugin"
    assert plugin.agents == (role,)
    assert plugin.options is _Options
    with pytest.raises(ValueError, match="duplicate agent role IDs"):
        _plugin(role, role)


def test_session_rejects_invalid_member_id_before_creation() -> None:
    async def scenario() -> None:
        role = _role()
        host = FakeRunHost(_plugin(role))
        with pytest.raises(ValueError, match="invalid agent member ID"):
            await host.agents.create_session(
                role, workspace=_workspace(), member_id="invalid member"
            )
        assert host.agents.sessions == ()

    asyncio.run(scenario())


def test_plugin_rejects_invalid_id() -> None:
    with pytest.raises(ValueError, match="invalid orchestration plugin ID"):
        OrchestrationPlugin(
            id="Invalid Plugin",
            agents=(_role(),),
            options=_Options,
            orchestrate=_orchestrate,
        )
