"""Every agent container of a Docker run can reach the run's evaluation socket.

Regression for #1593: the socket sat at a host path no container mounted, so the
evaluation tool failed with ``FileNotFoundError`` inside the container for every
provider. The run opens real ``DockerSandbox`` objects over a fake Docker daemon;
the daemon records each container's bind mounts.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.plugin import EmptyOptions
from tests.vibesys.run.test_core_host_composition import _plugin, _request, _write_project
from tests.vibesys.run.test_docker_candidate_sandboxes import _DaemonBackend

from vibesys.api.request import RunEnvironmentSpec
from vibesys.run.evaluation_socket import evaluation_socket_path
from vibesys.run.host import open_product_core_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import AgentCapabilities
from vs_agent.api.testing import FakeAgentClient, FakeAgentInvocationStore, FakeDockerBuildRunner
from vs_runtime.api.core import RunTiming
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api.testing import VirtualClock, run_virtual


def _containers_that_reach_the_socket(root: Path, candidates: int) -> list[bool]:
    """For the root container and each candidate's, whether the socket path is mounted in it."""
    project_root = root / "project"
    templates = root / "templates"
    templates.mkdir()
    _write_project(project_root)
    engine = FakeDockerEngine(root / "engine")

    def backend_factory(
        _name: object, *, log_dir: Path, log: object = None, image: str | None = None
    ) -> _DaemonBackend:
        made = _DaemonBackend(log_dir, log=log, image=image)  # ty: ignore[invalid-argument-type]
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
    integration = LocalRunIntegration()
    clock = VirtualClock(at=0.0)

    def reaches(run_id: str) -> list[bool]:
        socket = evaluation_socket_path(project_root, run_id)
        return [
            any(
                container_path == str(host_path) == str(socket.parent)
                for container_path, host_path in container.mounts.items()
            )
            for container in engine.containers()
        ]

    async def exercise() -> list[bool]:
        async with open_product_core_host(
            request,
            integration,
            plugin=_plugin(templates),
            options=EmptyOptions(),
            timing=RunTiming(clock, 60.0),
            agent_client_factory=lambda **_: FakeAgentClient(
                capabilities=AgentCapabilities(provider_session_resume=True)
            ),
            backend_factory=backend_factory,
            invocation_store_factory=lambda _state, _key: FakeAgentInvocationStore(),
        ) as host:
            for _ in range(candidates):
                await host.run.workspaces.create_candidate()
            (run_id,) = {c.labels["vibesys.run-id"] for c in engine.containers()}
            return reaches(run_id)

    try:
        return run_virtual(clock, exercise())
    finally:
        integration.close()


@settings(max_examples=3, deadline=None)
@given(candidates=st.integers(min_value=0, max_value=3))
def test_every_agent_container_mounts_the_evaluation_socket_directory(candidates: int) -> None:
    previous = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = "test-key"
    try:
        with tempfile.TemporaryDirectory() as directory:
            reached = _containers_that_reach_the_socket(Path(directory), candidates)
    finally:
        if previous is None:
            del os.environ["ANTHROPIC_API_KEY"]
        else:
            os.environ["ANTHROPIC_API_KEY"] = previous

    assert len(reached) >= 1 + candidates
    assert all(reached)
