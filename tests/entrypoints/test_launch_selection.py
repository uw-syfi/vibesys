"""Explicit optional implementation selection preserves falsey values."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from tests.entrypoints.test_launch_frontends import _request
from tests.support.core_plugins import failing_plugin
from tests.support.real_git import NO_GIT, REAL_GIT, REAL_PROCESSES

from entrypoints.production import production_timing
from launch import (
    LaunchSettings,
    built_in_orchestrations,
    default_runs,
    open_run_store,
    validate_descriptor,
    validate_run_request,
)
from launch.agents import BuiltInSessionAgents
from vibesys.api import AuxiliaryAgentLaunch, OrchestrationRegistry, RunReady
from vs_agent.api import AgentCapabilities, AgentSessionKey
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore
from vs_project.api import Project
from vs_project.api.testing import FakeGitRepositories
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import FakePipedProcessLauncher, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import AuxiliaryAgents, ManagedAgent
    from vibesys.api.wiring import AuxiliaryResources
    from vs_agent.api import AgentEventSink, AgentInvocationStore
    from vs_runtime.api.infrastructure import RunState, ScopedAgentEnvironment
    from vs_sandbox.api import HostResource
    from vs_sim.api import Network


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
        client = FakeAgentClient(
            session_reuse=True,
            capabilities=AgentCapabilities(provider_session_resume=True),
            threads=OsThreads(),
        ).set_text(None, "selected fake")
        self.clients.append(client)
        return client


class FalseyFakeBackendFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def __call__(self, *_args: object, **_kwargs: object) -> FakeComputeBackend:
        self.calls += 1
        return FakeComputeBackend(threads=SimThreads())


class FalseyFakeSessionAgents:
    def __init__(self, client_factory: FalseyFakeAgentFactory, network: Network) -> None:
        self.implementation = BuiltInSessionAgents(
            client_factory=client_factory,
            network=network,
            git=NO_GIT,
            registry=built_in_orchestrations(),
        )
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def open_environment(
        self,
        resources: AuxiliaryResources,
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
        resources: AuxiliaryResources,
        agent_events: AgentEventSink,
    ) -> ManagedAgent:
        self.calls += 1
        return self.implementation.create_agent(launch, resources, agent_events)


def test_launch_preserves_falsey_injected_implementations(tmp_path: Path) -> None:
    registry = FalseyOrchestrationRegistry()
    registry.register_plugin(failing_plugin("launch-test"))
    request = _request(tmp_path / "project")
    agents_factory = FalseyFakeAgentFactory()
    backend_factory = FalseyFakeBackendFactory()
    network = UnixNetwork()
    agents = FalseyFakeSessionAgents(agents_factory, network)
    settings = FalseyLaunchSettings(
        timing=production_timing(),
        processes=REAL_PROCESSES,
        piped_processes=FakePipedProcessLauncher(),
        registry=registry,
        agent_client_factory=agents_factory,
        backend_factory=backend_factory,
        agents=agents,
        git_repository=FakeGitRepositories().repository,
        blocking=ThreadBlockingRunner(),
        network=network,
    )
    validate_descriptor(request.orchestration, registry=registry)
    validate_run_request(request, registry=registry, git=REAL_GIT)

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
                    provider="codex",
                    model="gpt-test",
                    system_prompt="Inspect the recorded run.",
                )
            )

        handle.session.on_ready(ready)
        try:
            result = await handle.result()
            assert backend_factory.calls == 1
            assert agents.calls == 1
            # The auxiliary agent's client, then the run's own: each from the injected factory.
            assert len(agents_factory.clients) == 2
            assert not agents_factory.clients[0].closed
            assert agents_factory.clients[1].closed
            assert (
                open_run_store(
                    Project.open(request.project_root),
                    registry=registry,
                    git=REAL_GIT,
                    threads=SimThreads(),
                )
                .get_run(result.run_id)
                .run_id
                == result.run_id
            )
        finally:
            for scope in scopes:
                scope.close()
        assert all(client.closed for client in agents_factory.clients)

    asyncio.run(execute())


class FalseyInvocationStoreFactory:
    def __init__(self) -> None:
        self.keys: list[AgentSessionKey] = []
        self.store = FakeAgentInvocationStore()

    def __bool__(self) -> bool:
        return False

    def __call__(self, _state: RunState, key: AgentSessionKey) -> AgentInvocationStore:
        self.keys.append(key)
        return self.store


@pytest.mark.parametrize("injected", [False, True])
def test_launch_wires_durable_invocation_store(tmp_path: Path, *, injected: bool) -> None:
    factory = FalseyInvocationStoreFactory()
    registry = OrchestrationRegistry()
    registry.register_plugin(failing_plugin("launch-test"))
    request = _request(tmp_path / "project")
    runs = default_runs(
        LaunchSettings(
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            timing=production_timing(),
            processes=REAL_PROCESSES,
            piped_processes=FakePipedProcessLauncher(),
            registry=registry,
            agent_client_factory=lambda **_kwargs: FakeAgentClient(
                capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
            ),
            backend_factory=lambda *_args, **_kwargs: FakeComputeBackend(threads=SimThreads()),
            git_repository=FakeGitRepositories().repository,
            invocation_store_factory=factory if injected else None,
        )
    )

    async def execute() -> None:
        result = await runs.start(request).result()
        assert result.loop == "launch-test"
        if injected:
            # One run-scoped journal serves every core session.
            assert factory.keys == [AgentSessionKey.for_member("core", "run")]

    asyncio.run(execute())
