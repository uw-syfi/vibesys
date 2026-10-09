"""A container-topology task through the Docker run environment, on a fake daemon.

``DockerEnvironment`` is driven through ``open_run_environment`` with a real
``DockerSandbox`` over ``FakeDockerEngine`` and a recording image-build runner,
so nothing patches the code under test.
"""

from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING

import pytest

from vibesys.constants import ComputeBackend
from vibesys.run.environment import open_run_environment
from vs_agent.api import CONTAINER_RUNTIME_TOOLCHAIN
from vs_runtime.api.infrastructure import (
    DockerEnvironment,
    DockerEnvironmentConfig,
    DockerInDockerUnsupportedError,
    LocalEnvironment,
    RunEnvironmentRequest,
    RunEnvironmentSpec,
    SkyPilotEnvironment,
    TrustedEvaluatorRequirements,
    build_run_environment,
)
from vs_sandbox.api import (
    ContainerRuntimeUnavailableError,
    DockerSandbox,
    SandboxKind,
)
from vs_sandbox.api.testing import FakeDockerEngine

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vs_sandbox.api import CommandRunner, HostResource

_IMAGE_ID = "sha256:" + "c" * 64


class _RecordingBuildRunner:
    """Answers ``docker build`` and ``docker image inspect`` without a daemon."""

    def __init__(self) -> None:
        self.argvs: list[tuple[str, ...]] = []

    def run(
        self, argv: Sequence[str], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        del cwd, timeout
        self.argvs.append(tuple(argv))
        stdout = _IMAGE_ID if argv[1] == "image" else ""
        return subprocess.CompletedProcess(tuple(argv), 0, stdout, "")


class _DaemonBackend:
    """A compute backend whose Docker sandboxes talk to a :class:`FakeDockerEngine`."""

    image = "base-image"
    name = ComputeBackend.CPU

    def __init__(self, engine: FakeDockerEngine) -> None:
        self.engine = engine
        self.creations: list[bool] = []
        self.accelerator_requests: list[bool] = []

    def make_sandbox(
        self,
        kind: SandboxKind,
        *,
        host_workspace: str,
        docker_in_docker: bool = False,
        container_image: str | None = None,
        resources: Sequence[HostResource] = (),
        **_other_keywords: object,
    ) -> CommandRunner:
        assert kind is SandboxKind.DOCKER
        self.creations.append(docker_in_docker)
        self.accelerator_requests.append(bool(_other_keywords.get("attach_accelerator", True)))
        return DockerSandbox(
            host_workspace=host_workspace,
            image=container_image or self.image,
            resources=resources,
            docker=self.engine,
            docker_in_docker=docker_in_docker,
        )

    def make_monitor(self, log_dir: Path) -> None:
        del log_dir

    def reselect_device(self) -> None:
        return


def _request(
    tmp_path: Path,
    backend: _DaemonBackend,
    *,
    docker_in_docker: bool = False,
    benchmark_command: str | None = None,
) -> RunEnvironmentRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(exist_ok=True)
    return RunEnvironmentRequest(
        log_dir=log_dir,
        workspace=workspace,
        ref_dir=None,
        backend=backend,
        agent_backend="stub",
        cli_provider=None,
        run_id="run-1",
        framework_root=tmp_path / "framework",
        benchmark_command=benchmark_command,
        evaluator_requirements=TrustedEvaluatorRequirements(),
        docker_in_docker=docker_in_docker,
    )


def _engine(tmp_path: Path, runtimes: Sequence[str]) -> FakeDockerEngine:
    return FakeDockerEngine(
        tmp_path / "engine", agent_ids=(os.getuid(), os.getgid()), runtimes=runtimes
    )


def _environment(engine: FakeDockerEngine, runner: _RecordingBuildRunner) -> DockerEnvironment:
    return DockerEnvironment(DockerEnvironmentConfig(docker=engine, build_runner=runner))


def test_a_docker_in_docker_task_gets_the_runtime_layer_and_a_same_path_workspace(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, ("runc", "sysbox-runc"))
    runner = _RecordingBuildRunner()
    backend = _DaemonBackend(engine)
    request = _request(
        tmp_path,
        backend,
        docker_in_docker=True,
        benchmark_command="python ${PROJECT_ROOT}/bench.py",
    )

    session = open_run_environment(_environment(engine, runner), request)
    try:
        build = next(argv for argv in runner.argvs if argv[1] == "build")
        assert f"TOOLCHAINS={CONTAINER_RUNTIME_TOOLCHAIN}" in build
        run = next(call for call in engine.calls if call[1] == "run")
        mounts = [run[i + 1] for i, flag in enumerate(run) if flag == "-v"]
        assert f"{request.workspace}:{request.workspace}" in mounts
        assert "--runtime" in run
        assert not any("docker.sock" in token for token in run)
        assert backend.creations == [True]
        assert session.view.paths.benchmark_command == f"python {request.workspace}/bench.py"
    finally:
        session.close()


def test_an_ordinary_task_keeps_the_workspace_mount_and_an_unchanged_image(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, ("runc",))
    runner = _RecordingBuildRunner()
    backend = _DaemonBackend(engine)
    request = _request(tmp_path, backend)

    session = open_run_environment(_environment(engine, runner), request)
    try:
        build = next(argv for argv in runner.argvs if argv[1] == "build")
        assert "TOOLCHAINS=" in build
        run = next(call for call in engine.calls if call[1] == "run")
        assert f"{request.workspace}:/workspace" in run
        assert "info" not in [call[1] for call in engine.calls]
    finally:
        session.close()


def test_a_host_without_sysbox_fails_before_any_image_is_built(tmp_path: Path) -> None:
    engine = _engine(tmp_path, ("runc",))
    runner = _RecordingBuildRunner()
    backend = _DaemonBackend(engine)

    with pytest.raises(ContainerRuntimeUnavailableError, match="docker_in_docker"):
        open_run_environment(
            _environment(engine, runner), _request(tmp_path, backend, docker_in_docker=True)
        )

    assert runner.argvs == []
    assert backend.creations == []


@pytest.mark.parametrize("name", ["modal", "skypilot", "local"])
def test_other_environments_reject_docker_in_docker_naming_the_key(
    tmp_path: Path, name: str
) -> None:
    backend = _DaemonBackend(_engine(tmp_path, ("runc", "sysbox-runc")))
    environment = (
        SkyPilotEnvironment.from_options({"profile": "p", "profiles_file": "x"})
        if name == "skypilot"
        else LocalEnvironment()
        if name == "local"
        else build_run_environment(RunEnvironmentSpec("modal", {"gpu": "A10G"}))
    )

    with pytest.raises(DockerInDockerUnsupportedError, match="docker_in_docker"):
        environment.prepare(_request(tmp_path, backend, docker_in_docker=True))


def test_a_sysbox_sandbox_is_cpu_only_and_an_ordinary_one_keeps_the_backend_accelerators(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, ("runc", "sysbox-runc"))
    for docker_in_docker, expected in ((True, False), (False, True)):
        backend = _DaemonBackend(engine)
        session = open_run_environment(
            _environment(engine, _RecordingBuildRunner()),
            _request(tmp_path, backend, docker_in_docker=docker_in_docker),
        )
        session.close()
        assert backend.accelerator_requests == [expected]
