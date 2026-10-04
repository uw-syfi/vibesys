"""Explicit optional implementation selection preserves falsey values."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from tests.entrypoints.test_launch_frontends import _request
from tests.vibesys.orchestration.plugin import EmptyOptions

from launch import (
    LaunchSettings,
    default_runs,
    open_run_store,
    validate_descriptor,
    validate_run_request,
)
from launch.agents import BuiltInSessionAgents
from vibesys.api import AuxiliaryAgentLaunch, OrchestrationRegistry, RunReady
from vs_agent.api.testing import FakeAgentClient
from vs_project.api import Project
from vs_runtime.api import AgentRole, OrchestrationPlugin, Run, RunStatus
from vs_sandbox.api.testing import FakeComputeBackend

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import BaseModel

    from vibesys.api import AuxiliaryAgents, ManagedAgent
    from vibesys.api.wiring import RunResources
    from vs_agent.api import AgentEventSink
    from vs_runtime.api.infrastructure import ScopedAgentEnvironment
    from vs_sandbox.api import HostResource


class FalseyOrchestrationRegistry(OrchestrationRegistry):
    def __bool__(self) -> bool:
        return False


class FalseyLaunchSettings(LaunchSettings):
    def __bool__(self) -> bool:
        return False


class FalseyFakeAgentFactory:
    def __init__(self) -> None:
        self.clients: list[FakeAgentClient] = []

    def __bool__(self) -> bool:
        return False

    def __call__(self, **_kwargs: object) -> FakeAgentClient:
        client = FakeAgentClient(session_reuse=True).set_text(None, "selected fake")
        self.clients.append(client)
        return client


class FalseyFakeBackendFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def __call__(self, *_args: object, **_kwargs: object) -> FakeComputeBackend:
        self.calls += 1
        return FakeComputeBackend()


class FalseyFakeSessionAgents:
    def __init__(self, client_factory: FalseyFakeAgentFactory) -> None:
        self.implementation = BuiltInSessionAgents(client_factory=client_factory)
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def open_environment(
        self,
        resources: RunResources,
        *,
        mounts: tuple[HostResource, ...] = (),
        agent_backend: str | None = None,
        cli_provider: str | None = None,
    ) -> ScopedAgentEnvironment:
        return self.implementation.open_environment(
            resources,
            mounts=mounts,
            agent_backend=agent_backend,
            cli_provider=cli_provider,
        )

    def create_agent(
        self,
        launch: AuxiliaryAgentLaunch,
        resources: RunResources,
        agent_events: AgentEventSink,
    ) -> ManagedAgent:
        self.calls += 1
        return self.implementation.create_agent(launch, resources, agent_events)


_ROLE = AgentRole(id="falsey-test", system_prompt="Return the scripted answer.")


async def _exercise(run: Run, _options: BaseModel) -> RunStatus:
    session = await run.agents.create_session(_ROLE, workspace=run.workspaces.root)
    assert await session.turn("A fake turn") == "selected fake"
    return RunStatus.SUCCEEDED


def test_launch_preserves_falsey_injected_implementations(tmp_path: Path) -> None:
    registry = FalseyOrchestrationRegistry()
    registry.register_plugin(
        OrchestrationPlugin(
            id="launch-test", agents=(_ROLE,), options=EmptyOptions, orchestrate=_exercise
        )
    )
    request = _request(tmp_path / "project")
    agents_factory = FalseyFakeAgentFactory()
    backend_factory = FalseyFakeBackendFactory()
    agents = FalseyFakeSessionAgents(agents_factory)
    settings = FalseyLaunchSettings(
        registry=registry,
        agent_client_factory=agents_factory,
        backend_factory=backend_factory,
        agents=agents,
    )
    validate_descriptor(request.orchestration, registry=registry)
    validate_run_request(request, registry=registry)

    async def execute() -> None:
        handle = default_runs(settings).start(request)
        scopes: list[AuxiliaryAgents] = []

        def ready(_ready: RunReady) -> None:
            scope = handle.session.open_auxiliary_agents()
            scopes.append(scope)
            scope.create_auxiliary_agent(
                AuxiliaryAgentLaunch(
                    role="chat",
                    member_id="falsey-selection",
                    driver="agentshim",
                    provider="codex",
                    model="gpt-test",
                    system_prompt="Inspect the recorded run.",
                )
            )

        handle.session.on_ready(ready)
        try:
            result = await handle.result()
            assert result.succeeded
            assert backend_factory.calls == 1
            assert agents.calls == 1
            assert len(agents_factory.clients) == 2
            assert not agents_factory.clients[0].closed
            assert agents_factory.clients[0].calls == []
            assert len(agents_factory.clients[1].calls) == 1
            assert agents_factory.clients[1].closed
            assert (
                open_run_store(Project.open(request.project_root), registry=registry)
                .get_run(result.run_id)
                .run_id
                == result.run_id
            )
        finally:
            for scope in scopes:
                scope.close()
        assert all(client.closed for client in agents_factory.clients)

    asyncio.run(execute())
