"""A core run's agent client reaches each candidate's own container.

Regression for #1552: the core run built one client from the root container, so
a turn in a candidate workspace ran ``docker exec -w`` on a directory the root
container does not have. The run opens real ``DockerSandbox`` objects over a
fake Docker daemon; the client factory records what the host gives the client.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from tests.support.real_git import NO_GITHUB, REAL_GIT
from tests.vibesys.orchestration.plugin import EmptyOptions
from tests.vibesys.run.test_core_host_composition import _plugin, _request, _write_project
from tests.vibesys.run.test_docker_candidate_sandboxes import _DaemonBackend

from vibesys.api.request import RunEnvironmentSpec
from vibesys.api.testing import FakeProfilerPreflight
from vibesys.run.host import open_product_core_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import AgentCapabilities, build_agent_client
from vs_agent.api.testing import (
    FakeAgentClient,
    FakeAgentInvocationStore,
    FakeDockerBuildRunner,
    FakeExecutor,
    stream_peers,
)
from vs_runtime.api import AgentWorkspaceRouteError
from vs_runtime.api.core import RunTiming
from vs_runtime.api.testing import FakeStopTimer, idle_environment_system
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    ManualClock,
    VirtualClock,
    run_virtual,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import AgentClientProtocol
    from vs_sandbox.api import DockerSandbox


def _sandboxes_seen_by_the_client(root: Path, candidates: int) -> tuple[bool, list[str]]:
    """Return whether the root path is served by the role's sandbox, and each candidate's mapping."""
    project_root = root / "project"
    templates = root / "templates"
    templates.mkdir()
    _write_project(project_root)
    engine = FakeDockerEngine(root / "engine", launcher=FakePipedProcessLauncher())
    given_to_client: dict[str, object] = {}

    def client_factory(**kwargs: object) -> FakeAgentClient:
        given_to_client.update(kwargs)
        return FakeAgentClient(
            capabilities=AgentCapabilities(provider_session_resume=True), threads=OsThreads()
        )

    def backend_factory(
        _name: object, *, log_dir: Path, log: object = None, image: str | None = None, clock: object
    ) -> _DaemonBackend:
        del clock  # the fake backend records no timestamps
        made = _DaemonBackend(
            log_dir,
            log=log,  # ty: ignore[invalid-argument-type]
            image=image,
            threads=OsThreads(),
            launcher=FakePipedProcessLauncher(),
        )
        made.engine = engine
        return made

    request = _request(project_root).model_copy(
        update={
            "agent_backend": "cli",
            "run_environment": RunEnvironmentSpec(
                "docker", {"build_runner": FakeDockerBuildRunner(), "docker": engine}
            ),
        }
    )
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    clock = VirtualClock(at=0.0)

    async def exercise() -> tuple[bool, list[str]]:
        async with open_product_core_host(
            request,
            integration,
            plugin=_plugin(templates),
            options=EmptyOptions(),
            timing=RunTiming(clock, 60.0, OsThreads()),
            agent_client_factory=client_factory,
            backend_factory=backend_factory,
            invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
            stop_timer=FakeStopTimer(threads=OsThreads()),
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            profiler_preflight=FakeProfilerPreflight(),
        ) as host:
            lookup = cast(
                "Callable[[Path], DockerSandbox | None]", given_to_client["workspace_sandboxes"]
            )
            roots = given_to_client["backends"]
            assert isinstance(roots, dict)
            root_served = lookup(host.workspaces.root.path) is None
            mapped: list[str] = []
            for _ in range(candidates):
                candidate = await host.workspaces.create_candidate()
                path = candidate.path
                sandbox: DockerSandbox | None = lookup(path)
                assert sandbox is not None
                assert sandbox is not roots["implementer"]
                mapped.append(sandbox.agent_path(path))
                await candidate.discard()
                with pytest.raises(AgentWorkspaceRouteError):
                    lookup(path)
            return root_served, mapped

    try:
        return run_virtual(clock, exercise())
    finally:
        integration.close()


def test_the_client_resolves_each_candidate_to_its_own_container() -> None:
    """Two candidates, each with its own container (Budget: a whole host over a fake daemon)."""
    candidates = 2
    # The container receives the provider credential, so the run needs one to open.
    previous = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        with tempfile.TemporaryDirectory() as directory:
            root_served, mapped = _sandboxes_seen_by_the_client(Path(directory), candidates)
    finally:
        if previous is None:
            del os.environ["ANTHROPIC_API_KEY"]
        else:
            os.environ["ANTHROPIC_API_KEY"] = previous

    assert root_served
    assert mapped == ["/workspace"] * candidates


@dataclass
class _Turns:
    """Where each turn of one run exec'd, and which containers the daemon held."""

    targets: list[str] = field(default_factory=list)
    workdirs: list[str] = field(default_factory=list)
    root_containers: set[str] = field(default_factory=set)
    candidate_containers: set[str] = field(default_factory=set)


def _turns_through_the_public_builder(root: Path, candidates: int) -> _Turns:
    """Run one turn in the root and in each candidate with the client the run builds."""
    project_root = root / "project"
    templates = root / "templates"
    templates.mkdir()
    _write_project(project_root)
    engine = FakeDockerEngine(root / "engine", launcher=FakePipedProcessLauncher())
    executor = FakeExecutor(
        [],
        peers=stream_peers("claude", *(["ok"] * (candidates + 1))).build,
    )
    clients: list[AgentClientProtocol] = []

    def client_factory(**kwargs: object) -> AgentClientProtocol:
        client = build_agent_client(
            executor_factory=lambda: executor,
            threads=OsThreads(),
            **kwargs,  # ty: ignore[invalid-argument-type]
        )
        clients.append(client)
        return client

    def backend_factory(
        _name: object, *, log_dir: Path, log: object = None, image: str | None = None, clock: object
    ) -> _DaemonBackend:
        del clock  # the fake backend records no timestamps
        made = _DaemonBackend(
            log_dir,
            log=log,  # ty: ignore[invalid-argument-type]
            image=image,
            threads=OsThreads(),
            launcher=FakePipedProcessLauncher(),
        )
        made.engine = engine
        return made

    request = _request(project_root).model_copy(
        update={
            "agent_backend": "cli",
            "run_environment": RunEnvironmentSpec(
                "docker", {"build_runner": FakeDockerBuildRunner(), "docker": engine}
            ),
        }
    )
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    clock = VirtualClock(at=0.0)
    turns = _Turns()

    async def exercise() -> None:
        async with open_product_core_host(
            request,
            integration,
            plugin=_plugin(templates),
            options=EmptyOptions(),
            timing=RunTiming(clock, 60.0, OsThreads()),
            agent_client_factory=client_factory,
            backend_factory=backend_factory,
            invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
            stop_timer=FakeStopTimer(threads=OsThreads()),
            blocking=ThreadBlockingRunner(),
            network=UnixNetwork(),
            profiler_preflight=FakeProfilerPreflight(),
        ) as host:
            turns.root_containers = {c.container_id for c in engine.containers()}
            paths = [
                host.workspaces.root.path,
                *[(await host.workspaces.create_candidate()).path for _ in range(candidates)],
            ]
            turns.candidate_containers = {
                c.container_id for c in engine.containers()
            } - turns.root_containers
            for path in paths:
                await asyncio.to_thread(
                    clients[0].invoke_text,
                    kind="implementer",
                    workspace=path,
                    system_prompt="Implement.",
                    user_prompt="go",
                    round_label="r",
                )

    try:
        run_virtual(clock, exercise())
    finally:
        integration.close()
    known = turns.root_containers | turns.candidate_containers
    for spawn in executor.spawns:
        argv = list(spawn.argv)
        turns.targets.append(next(part for part in argv if part in known))
        turns.workdirs.append(argv[argv.index("-w") + 1])
    return turns


def test_a_candidates_turn_execs_in_that_candidates_container() -> None:
    candidates = 2
    previous = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        with tempfile.TemporaryDirectory() as directory:
            turns = _turns_through_the_public_builder(Path(directory), candidates)
    finally:
        if previous is None:
            del os.environ["ANTHROPIC_API_KEY"]
        else:
            os.environ["ANTHROPIC_API_KEY"] = previous

    assert turns.targets[0] in turns.root_containers
    assert set(turns.targets[1:]) == turns.candidate_containers
    assert len(set(turns.targets[1:])) == candidates
    assert set(turns.workdirs) == {"/workspace"}
