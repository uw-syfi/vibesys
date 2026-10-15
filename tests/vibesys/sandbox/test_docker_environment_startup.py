"""Opening the Docker run environment reports its startup stages and the image build's progress."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.run.environment import open_run_environment
from vs_agent.api.images import ImageBuildProgress, TaskImageBuildError
from vs_agent.api.testing import FakeDockerBuildRunner, docker_result
from vs_runtime.api.infrastructure import (
    DockerEnvironment,
    DockerEnvironmentConfig,
    EnvironmentStartup,
    EnvironmentStartupStage,
    RunEnvironmentRequest,
    TrustedEvaluatorRequirements,
)
from vs_runtime.api.testing import DaemonBackend, daemon_engine, idle_environment_system
from vs_sim.api.testing import FakePipedProcessLauncher, ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

_BUILD_OUTPUT = """\
#1 [internal] load build definition from agent.Dockerfile
#5 [1/3] FROM docker.io/library/python:3.12-bookworm
#6 [2/3] RUN npm install -g
#7 [3/3] RUN pip install uv
#8 exporting to image
"""


def _open(
    tmp_path: Path, runner: FakeDockerBuildRunner, observed: list[EnvironmentStartup]
) -> None:
    engine = daemon_engine(tmp_path, launcher=FakePipedProcessLauncher())
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=DaemonBackend(engine, threads=SimThreads()),
        agent_backend="stub",
        cli_provider=None,
        run_id="run-1",
        framework_root=tmp_path / "framework",
        evaluator_requirements=TrustedEvaluatorRequirements(),
        startup=observed.append,
        clock=ManualClock(),
        system=idle_environment_system(),
    )
    environment = DockerEnvironment(DockerEnvironmentConfig(docker=engine, build_runner=runner))
    open_run_environment(environment, request).close()


def test_the_environment_reports_the_build_steps_then_the_container_start(
    tmp_path: Path,
) -> None:
    runner = FakeDockerBuildRunner(build_result=docker_result(stderr=_BUILD_OUTPUT))
    observed: list[EnvironmentStartup] = []

    _open(tmp_path, runner, observed)

    building = EnvironmentStartupStage.BUILDING_IMAGE
    assert observed == [
        EnvironmentStartup(stage=building),
        EnvironmentStartup(
            stage=building,
            progress=ImageBuildProgress(
                done=0, total=3, label="FROM docker.io/library/python:3.12-bookworm"
            ),
        ),
        EnvironmentStartup(
            stage=building, progress=ImageBuildProgress(done=1, total=3, label="RUN npm install -g")
        ),
        EnvironmentStartup(
            stage=building, progress=ImageBuildProgress(done=2, total=3, label="RUN pip install uv")
        ),
        EnvironmentStartup(stage=EnvironmentStartupStage.STARTING_CONTAINER),
    ]


def test_a_failed_build_never_reports_a_container_start(tmp_path: Path) -> None:
    runner = FakeDockerBuildRunner(
        build_result=docker_result(returncode=1, stderr="#6 [2/3] RUN npm install -g\nnpm ERR!")
    )
    observed: list[EnvironmentStartup] = []

    with pytest.raises(TaskImageBuildError, match="npm ERR!"):
        _open(tmp_path, runner, observed)

    assert observed == [
        EnvironmentStartup(stage=EnvironmentStartupStage.BUILDING_IMAGE),
        EnvironmentStartup(
            stage=EnvironmentStartupStage.BUILDING_IMAGE,
            progress=ImageBuildProgress(done=1, total=3, label="RUN npm install -g"),
        ),
    ]
