"""Explicit optional implementation selection preserves falsey values."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from tests.entrypoints.test_launch_frontends import _request
from tests.support.core_plugins import capability_plugin, core_policy, failing_plugin
from tests.support.skeleton_strategy import SkeletonStrategy

from launch import (
    LaunchSettings,
    default_runs,
    validate_descriptor,
)
from launch.agents import BuiltInSessionAgents
from vibesys.api import AuxiliaryAgentLaunch, OrchestrationRegistry, RunReady, RunStatus
from vs_agent.api import AgentCapabilities, AgentSessionKey, Completed
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore, FakeInvocation
from vs_project.api.testing import FakeGitRepositories, FakeGitRepository
from vs_runtime.api import AgentRole
from vs_runtime.api.core import PRODUCTION_LEASE_SECONDS, RunTiming
from vs_runtime.api.testing import FakeStopTimer
from vs_sandbox.api import SandboxKind
from vs_sandbox.api.testing import FakeComputeBackend, FakeLifecycleRunner
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    FakeProcesses,
    InlineBlockingRunner,
    ProcessScript,
    Sim,
    SimNetwork,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.api import AuxiliaryAgents, ManagedAgent
    from vibesys.api.wiring import RunResources
    from vs_agent.api import AgentEventSink, AgentInvocationStore
    from vs_project.api import GitFaultSink
    from vs_runtime.api.infrastructure import RunState, ScopedAgentEnvironment
    from vs_sandbox.api import HostResource
    from vs_sim.api import Threads


_IMPLEMENTER = AgentRole(id="skeleton-implementer", system_prompt="Implement the requested change.")


class FalseyOrchestrationRegistry(OrchestrationRegistry):
    def __bool__(self) -> bool:
        return False


class FalseyLaunchSettings(LaunchSettings):
    def __bool__(self) -> bool:
        return False


class FalseyFakeAgentFactory:
    def __init__(self, threads: Threads) -> None:
        self.threads = threads
        self.clients: list[FakeAgentClient] = []

    def __bool__(self) -> bool:
        return False

    def __call__(self, **_kwargs: object) -> FakeAgentClient:
        client = FakeAgentClient(
            session_reuse=True,
            capabilities=AgentCapabilities(provider_session_resume=True),
            threads=self.threads,
        ).set_text(None, "selected fake")
        self.clients.append(client)
        return client


class FalseyFakeBackendFactory:
    def __init__(self, threads: Threads) -> None:
        self.threads = threads
        self.calls = 0

    def __bool__(self) -> bool:
        return False

    def __call__(self, *_args: object, **_kwargs: object) -> FakeComputeBackend:
        self.calls += 1
        backend = FakeComputeBackend(threads=self.threads)
        backend.script_sandbox_for_kind(
            SandboxKind.DOCKER, FakeLifecycleRunner(threads=self.threads)
        )
        return backend


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


class FalseyInvocationStoreFactory:
    def __init__(self) -> None:
        self.keys: list[AgentSessionKey] = []
        self.store = FakeAgentInvocationStore()

    def __bool__(self) -> bool:
        return False

    def __call__(self, _state: RunState, key: AgentSessionKey) -> AgentInvocationStore:
        self.keys.append(key)
        return self.store


def _processes() -> FakeProcesses:
    return FakeProcesses(lambda _spec: ProcessScript(), pass_time=lambda _seconds: None)


class _WorktreeMarkerRepository(FakeGitRepository):
    """Give linked Fake worktrees the ``.git`` marker real Git creates."""

    def add_worktree(self, destination: Path, commit: str) -> None:
        super().add_worktree(destination, commit)
        (destination / ".git").touch()


class _WorktreeMarkerRepositories(FakeGitRepositories):
    """Factory for the production-shaped linked-worktree Fake above."""

    def repository(self, root: Path, faults: GitFaultSink | None = None) -> FakeGitRepository:
        if faults is None:
            return super().repository(root)
        return _WorktreeMarkerRepository(root, self, faults=faults)


def test_launch_preserves_falsey_injected_implementations(tmp_path: Path, sim: Sim) -> None:
    registry = FalseyOrchestrationRegistry()
    registry.register_plugin(failing_plugin("launch-test"))
    request = _request(tmp_path / "project")
    threads = sim.threads()
    agents_factory = FalseyFakeAgentFactory(threads)
    backend_factory = FalseyFakeBackendFactory(threads)
    agents = FalseyFakeSessionAgents(agents_factory)
    invocation_stores = FalseyInvocationStoreFactory()
    settings = FalseyLaunchSettings(
        timing=RunTiming(sim.clock, PRODUCTION_LEASE_SECONDS, threads),
        processes=_processes(),
        piped_processes=FakePipedProcessLauncher(),
        registry=registry,
        agent_client_factory=agents_factory,
        backend_factory=backend_factory,
        agents=agents,
        invocation_store_factory=invocation_stores,
        git_repository=FakeGitRepositories().repository,
        stop_timer=FakeStopTimer(threads=threads),
        blocking=InlineBlockingRunner(),
        network=SimNetwork(threads),
    )
    validate_descriptor(request.orchestration, registry=registry)
    runs = default_runs(settings)

    async def execute() -> None:
        handle = runs.start(request)
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
            # The zero-role core opens no provider client or invocation journal; the one
            # client belongs solely to the explicitly requested auxiliary conversation.
            assert len(agents_factory.clients) == 1
            assert not agents_factory.clients[0].closed
            assert invocation_stores.keys == []
            assert runs.attach(result.run_id).run_id == result.run_id
        finally:
            for scope in scopes:
                scope.close()
        assert all(client.closed for client in agents_factory.clients)

    sim.run(execute(), threads=threads)


@pytest.mark.parametrize("injected", [False, True])
def test_launch_wires_durable_invocation_store(tmp_path: Path, sim: Sim, *, injected: bool) -> None:
    factory = FalseyInvocationStoreFactory()
    registry = OrchestrationRegistry()
    strategy = SkeletonStrategy.unmeasured(keeps_candidate=False)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "skeleton.j2").write_text("Make the candidate faster.\n", encoding="utf-8")
    (templates / "skeleton-resume.j2").write_text(
        "Continue improving the candidate.\n", encoding="utf-8"
    )
    plugin = capability_plugin(
        "launch-test",
        agents=(_IMPLEMENTER,),
        strategy=strategy,
    )
    plugin = replace(plugin, core=core_policy(templates, strategy))
    registry.register_plugin(plugin)
    request = _request(tmp_path / "project")
    threads = sim.threads()
    repositories = _WorktreeMarkerRepositories()
    clients: list[FakeAgentClient] = []
    backend = FakeComputeBackend(threads=threads)
    backend.script_sandbox_for_kind(SandboxKind.DOCKER, FakeLifecycleRunner(threads=threads))

    def implement(invocation: FakeInvocation) -> dict[str, object]:
        (invocation.workspace / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
        repository = repositories.repository(invocation.workspace)
        repository.stage_all(["."])
        repository.commit("implement candidate")
        commit = repository.head()
        assert commit is not None
        return {"commit": commit}

    def agent_client(**_kwargs: object) -> FakeAgentClient:
        client = FakeAgentClient(
            capabilities=AgentCapabilities(provider_session_resume=True), threads=threads
        ).set_response(_IMPLEMENTER.id, implement)
        clients.append(client)
        return client

    runs = default_runs(
        LaunchSettings(
            blocking=InlineBlockingRunner(),
            network=SimNetwork(threads),
            timing=RunTiming(sim.clock, PRODUCTION_LEASE_SECONDS, threads),
            processes=_processes(),
            piped_processes=FakePipedProcessLauncher(),
            registry=registry,
            agent_client_factory=agent_client,
            backend_factory=lambda *_args, **_kwargs: backend,
            git_repository=repositories.repository,
            invocation_store_factory=factory if injected else None,
            stop_timer=FakeStopTimer(threads=threads),
        )
    )

    async def execute() -> None:
        result = await runs.start(request).result()
        assert result.loop == "launch-test"
        assert len(clients) == 1
        assert [call.kind for call in clients[0].calls] == [_IMPLEMENTER.id]
        assert result.status is RunStatus.COMPLETED
        if injected:
            # One run-scoped journal serves every core session.
            assert factory.keys == [AgentSessionKey.for_member("core", "run")]
            state = factory.store.load_optional()
            assert state is not None
            [record] = state.invocations.values()
            assert isinstance(record.outcome, Completed)

    sim.run(execute(), threads=threads)
