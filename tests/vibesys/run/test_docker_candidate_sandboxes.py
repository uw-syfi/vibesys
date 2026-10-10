"""The Docker run environment opens an isolated candidate sandbox per candidate.

Regression for #1549: Docker never reported ``supports_parallel_candidates``, so
the dynamic loop, whose every workstream runs in its own candidate sandbox,
refused the default run environment. The runs below open a real
``DockerSandbox`` over a fake Docker daemon, so the assertions are on the
daemon's own container list.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.plugin import capability_plugin

from vibesys.api import (
    ComputeBackend,
    Config,
    OrchestrationDescriptor,
    ProfilerKind,
    RunRequest,
)
from vibesys.api.request import RunEnvironmentSpec, load_input_bundle
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api.testing import FakeDockerBuildRunner
from vs_sandbox.api import DockerSandbox
from vs_sandbox.api.testing import FakeDockerEngine, HostExecutedContainerBackend

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sandbox.api import CommandRunner, HostResource, SandboxKind

_PLUGIN = capability_plugin("three-agent-rounds")


class _DaemonBackend(HostExecutedContainerBackend):
    """A backend whose Docker sandboxes talk to a :class:`FakeDockerEngine`."""

    engine: FakeDockerEngine

    def make_sandbox(
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        resources: Sequence[HostResource] = (),
        container_image: str | None = None,
        run_id: str | None = None,
        **_other: object,
    ) -> CommandRunner:
        del kind
        return DockerSandbox(
            host_workspace=host_workspace,
            image=container_image or "agent-image",
            resources=resources,
            docker=self.engine,
            run_id=run_id,
        )


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _candidate_container_counts(
    root: Path, *, backend: ComputeBackend, image: str | None
) -> tuple[bool, int, int, int]:
    """Return (supported, containers before, with a candidate, after discarding it)."""
    project_root = root / "project"
    _write_project(project_root)
    engine = FakeDockerEngine(root / "engine")
    options: dict[str, object] = {"build_runner": FakeDockerBuildRunner(), "docker": engine}
    if image is not None:
        options["image"] = image
    request = RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(
            id="three-agent-rounds", config_version=1, options={"rounds": 2}
        ),
        config=Config.model_validate({"model": {"name": "gpt-test"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="docker-candidates",
        run_environment=RunEnvironmentSpec("docker", options),
        agent_backend="stub",
        cli_provider="claude",
        profiler_kind=ProfilerKind.NONE,
        backend=backend,
    )

    def backend_factory(
        _name: object,
        *,
        log_dir: Path,
        log: object = None,
        image: str | None = None,
    ) -> _DaemonBackend:
        made = _DaemonBackend(log_dir, log=log, image=image)  # ty: ignore[invalid-argument-type]
        made.engine = engine
        return made

    integration = LocalRunIntegration()

    async def exercise() -> tuple[bool, int, int, int]:
        async with open_product_run_host(
            request, integration, plugin=_PLUGIN, backend_factory=backend_factory
        ) as run:
            supported = run.workspaces.supports_parallel_candidates
            before = len(engine.containers())
            candidate = await run.workspaces.create_candidate()
            with_candidate = len(engine.containers())
            await candidate.discard()
            return supported, before, with_candidate, len(engine.containers())

    try:
        return asyncio.run(exercise())
    finally:
        integration.close()


@settings(max_examples=6, deadline=None)
@given(
    backend=st.sampled_from([ComputeBackend.CPU, ComputeBackend.CUDA, ComputeBackend.ROCM]),
    image=st.sampled_from([None, "registry.example/custom-task:1"]),
)
def test_docker_run_opens_an_isolated_candidate_sandbox_per_candidate(
    backend: ComputeBackend, image: str | None
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        supported, before, with_candidate, after = _candidate_container_counts(
            Path(directory), backend=backend, image=image
        )

    assert supported
    assert with_candidate == before + 1
    assert after == before
