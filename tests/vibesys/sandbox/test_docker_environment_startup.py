"""Opening the Docker run environment reports its startup stages and the image build's progress."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

import pytest

from vibesys.log_owners import LOG_OWNER, StartupStep
from vibesys.run.environment import open_run_environment
from vibesys.run.logs import startup_output_opener
from vs_agent.api.images import ImageBuildProgress, TaskImageBuildError
from vs_agent.api.testing import FakeDockerBuildRunner, docker_result
from vs_logs.api import MemoryLogStore, StreamOutcome
from vs_logs.api.testing import MemoryVolume, no_warning
from vs_logs.read import Tail, reader_of
from vs_runtime.api.infrastructure import (
    DockerEnvironment,
    DockerEnvironmentConfig,
    EnvironmentStartup,
    EnvironmentStartupStage,
    EnvironmentStartupState,
    RunEnvironmentRequest,
    RunEnvironmentSessionRole,
    TrustedEvaluatorRequirements,
)
from vs_runtime.api.testing import DaemonBackend, daemon_engine, idle_environment_system
from vs_sandbox.api import SandboxKind
from vs_sandbox.api.evaluator_tools import CargoGitToolSpec, EvaluatorToolError
from vs_sandbox.api.testing import FakeComputeBackend, FakeLifecycleRunner
from vs_sim.api import Capture
from vs_sim.api.testing import FakePipedProcessLauncher, ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vibesys.log_owners import LogOwner
    from vs_logs.read import LogReader, StreamInfo
    from vs_runtime.api.infrastructure import StartupOutputOpener
    from vs_sandbox.api.testing import FakeDockerEngine
    from vs_sim.api import OutputSink

_BUILD_OUTPUT = """\
#1 [internal] load build definition from agent.Dockerfile
#5 [1/3] FROM docker.io/library/python:3.12-bookworm
#6 [2/3] RUN npm install -g
#7 [3/3] RUN pip install uv
#8 exporting to image
"""


def _environment(
    tmp_path: Path,
    runner: FakeDockerBuildRunner,
    observed: list[EnvironmentStartup],
    *,
    engine: FakeDockerEngine | None = None,
) -> tuple[DockerEnvironment, RunEnvironmentRequest]:
    engine = engine or daemon_engine(tmp_path, launcher=FakePipedProcessLauncher())
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
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
    return environment, request


def _open(
    tmp_path: Path,
    runner: FakeDockerBuildRunner,
    observed: list[EnvironmentStartup],
    *,
    startup_output: StartupOutputOpener | None = None,
    engine: FakeDockerEngine | None = None,
) -> None:
    environment, request = _environment(tmp_path, runner, observed, engine=engine)
    open_run_environment(environment, replace(request, startup_output=startup_output)).close()


def _finished(stage: EnvironmentStartupStage) -> EnvironmentStartup:
    """The observation that *stage* ended, after no time passed on the fake clock."""
    return EnvironmentStartup(
        stage=stage, state=EnvironmentStartupState.FINISHED, elapsed_seconds=0.0
    )


def test_the_environment_reports_each_stage_start_and_finish_around_the_build_steps(
    tmp_path: Path,
) -> None:
    runner = FakeDockerBuildRunner(build_result=docker_result(stderr=_BUILD_OUTPUT))
    observed: list[EnvironmentStartup] = []

    _open(tmp_path, runner, observed)

    building = EnvironmentStartupStage.BUILDING_IMAGE
    starting = EnvironmentStartupStage.STARTING_CONTAINER
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
        EnvironmentStartup(
            stage=building, progress=ImageBuildProgress(done=3, total=3, label="exporting to image")
        ),
        _finished(building),
        EnvironmentStartup(stage=starting),
        _finished(starting),
    ]


def test_a_failed_build_never_reports_a_finish_or_a_container_start(tmp_path: Path) -> None:
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


def _tool_requirements(tmp_path: Path) -> TrustedEvaluatorRequirements:
    tool = CargoGitToolSpec(
        kind="cargo-git",
        git="https://example.com/tools/check.git",
        rev="a" * 40,
        package="check-tool",
        bins=("check",),
    )
    return TrustedEvaluatorRequirements(
        package_root=tmp_path / "evaluator-package",
        tools={"check-tool": tool},
        tools_root=tmp_path / "operator-tools",
    )


def test_a_tool_build_is_a_stage_between_the_image_and_the_container_and_never_finishes_failed(
    tmp_path: Path,
) -> None:
    runner = FakeDockerBuildRunner(build_result=docker_result(stderr=_BUILD_OUTPUT))
    observed: list[EnvironmentStartup] = []
    environment, request = _environment(tmp_path, runner, observed)
    backend = FakeComputeBackend(threads=SimThreads())
    backend.script_sandbox(
        SandboxKind.DOCKER,
        str(request.log_dir / "evaluator-tool-builder-workspace"),
        FakeLifecycleRunner(),
    )
    request = replace(request, backend=backend, evaluator_requirements=_tool_requirements(tmp_path))

    # The scripted builder publishes no binary, so the build runs and then fails.
    with pytest.raises(EvaluatorToolError, match="did not publish every declared tool"):
        open_run_environment(environment, request)

    stages = [(item.stage, item.state) for item in observed if item.progress is None]
    assert stages == [
        (EnvironmentStartupStage.BUILDING_IMAGE, EnvironmentStartupState.RUNNING),
        (EnvironmentStartupStage.BUILDING_IMAGE, EnvironmentStartupState.FINISHED),
        (EnvironmentStartupStage.BUILDING_TOOLS, EnvironmentStartupState.RUNNING),
    ]


def test_each_container_of_a_run_is_told_once_by_name_and_role(tmp_path: Path) -> None:
    """Regression: every container start logged one identical line, so two read as one repeated."""
    runner = FakeDockerBuildRunner(build_result=docker_result(stderr=_BUILD_OUTPUT))
    log: list[str] = []
    environment, request = _environment(tmp_path, runner, [])
    request = replace(request, log=log.append)
    child = replace(request, session_role=RunEnvironmentSessionRole.CHILD, startup=None)

    with open_run_environment(environment, request), open_run_environment(environment, child):
        pass

    started = [line for line in log if "container" in line]
    assert len(started) == 2
    assert len(set(started)) == 2
    assert [("(root)" in line, "(child)" in line) for line in started] == [
        (True, False),
        (False, True),
    ]


@dataclass
class _Stream:
    stage: EnvironmentStartupStage
    command: str
    output: Capture = field(default_factory=lambda: Capture(limit=1 << 16))
    closed: bool = False


@dataclass
class _Streams:
    """An opener that records each stream, what reached it, and whether it was closed."""

    observed: list[EnvironmentStartup]
    opened: list[_Stream] = field(default_factory=list)
    #: Each stream's open, as the startup observations seen so far when it opened.
    seen_at_open: list[int] = field(default_factory=list)

    @contextmanager
    def __call__(self, stage: EnvironmentStartupStage, command: str) -> Iterator[OutputSink]:
        assert all(stream.closed for stream in self.opened), "streams never overlap"
        stream = _Stream(stage, command)
        self.opened.append(stream)
        self.seen_at_open.append(len(self.observed))
        try:
            yield stream.output
        finally:
            stream.closed = True


_PULL = "Unable to find image locally\nbase-image: Pulling from library/base\n"


def test_each_startup_command_writes_to_a_stream_of_its_own_that_is_closed(
    tmp_path: Path,
) -> None:
    runner = FakeDockerBuildRunner(build_result=docker_result(stderr=_BUILD_OUTPUT))
    observed: list[EnvironmentStartup] = []
    streams = _Streams(observed)
    engine = daemon_engine(tmp_path, launcher=FakePipedProcessLauncher())
    engine.pulls_on_run(_PULL)

    _open(tmp_path, runner, observed, startup_output=streams, engine=engine)

    build, start = streams.opened
    assert (build.stage, build.command) == (
        EnvironmentStartupStage.BUILDING_IMAGE,
        "docker build agent image on base-image",
    )
    assert (start.stage, start.command) == (
        EnvironmentStartupStage.STARTING_CONTAINER,
        "docker run",
    )
    assert build.closed
    assert start.closed
    assert _BUILD_OUTPUT.encode() in build.output.stderr
    assert start.output.stderr == _PULL.encode()
    assert start.output.stdout.strip()  # the new container's id
    # Each stream opens after its stage is reported.
    stage_at_open = [observed[seen - 1].stage for seen in streams.seen_at_open]
    assert stage_at_open == [
        EnvironmentStartupStage.BUILDING_IMAGE,
        EnvironmentStartupStage.STARTING_CONTAINER,
    ]


def test_a_failed_build_closes_its_stream_and_opens_no_other(tmp_path: Path) -> None:
    runner = FakeDockerBuildRunner(
        build_result=docker_result(returncode=1, stderr="#6 [2/3] RUN npm install -g\nnpm ERR!")
    )
    observed: list[EnvironmentStartup] = []
    streams = _Streams(observed)

    with pytest.raises(TaskImageBuildError, match="npm ERR!"):
        _open(tmp_path, runner, observed, startup_output=streams)

    (build,) = streams.opened
    assert build.stage is EnvironmentStartupStage.BUILDING_IMAGE
    assert build.closed
    assert b"npm ERR!" in build.output.stderr


def _read_all(reader: LogReader[LogOwner], info: StreamInfo[LogOwner]) -> bytes:
    return reader.read(info.stream_id, Tail(1 << 16), 1 << 16).data


def test_startup_output_reaches_the_run_log_store_under_its_step(tmp_path: Path) -> None:
    runner = FakeDockerBuildRunner(build_result=docker_result(stderr=_BUILD_OUTPUT))
    store = MemoryLogStore(LOG_OWNER, threads=SimThreads(), warn=no_warning)
    engine = daemon_engine(tmp_path, launcher=FakePipedProcessLauncher())
    engine.pulls_on_run(_PULL)

    _open(
        tmp_path,
        runner,
        [],
        startup_output=startup_output_opener(store),
        engine=engine,
    )

    reader = reader_of(store, LOG_OWNER)
    build, start = reader.list_streams()
    assert build.owner == StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE)
    assert start.owner == StartupStep(stage=EnvironmentStartupStage.STARTING_CONTAINER)
    assert build.command == "docker build agent image on base-image"
    assert start.command == "docker run"
    assert (build.outcome, start.outcome) == (StreamOutcome.ENDED, StreamOutcome.ENDED)
    assert _BUILD_OUTPUT.encode() in _read_all(reader, build)
    assert _PULL.encode() in _read_all(reader, start)


def test_a_failed_build_leaves_a_failed_stream_in_the_run_log_store(tmp_path: Path) -> None:
    runner = FakeDockerBuildRunner(
        build_result=docker_result(returncode=1, stderr="#6 [2/3] RUN npm install -g\nnpm ERR!")
    )
    store = MemoryLogStore(LOG_OWNER, threads=SimThreads(), warn=no_warning)

    with pytest.raises(TaskImageBuildError, match="npm ERR!"):
        _open(tmp_path, runner, [], startup_output=startup_output_opener(store))

    reader = reader_of(store, LOG_OWNER)
    (build,) = reader.list_streams()
    assert build.owner == StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE)
    assert build.outcome is StreamOutcome.FAILED
    assert b"npm ERR!" in _read_all(reader, build)


def test_a_full_log_disk_never_fails_the_image_build(tmp_path: Path) -> None:
    """The build's stream keeps what fit; the environment opens as if nothing went wrong."""
    runner = FakeDockerBuildRunner(build_result=docker_result(stderr=_BUILD_OUTPUT))
    volume = MemoryVolume()
    warnings: list[str] = []
    store = MemoryLogStore(LOG_OWNER, threads=SimThreads(), warn=warnings.append, volume=volume)
    engine = daemon_engine(tmp_path, launcher=FakePipedProcessLauncher())
    engine.pulls_on_run(_PULL)
    volume.capacity = 300  # the build's index record fits; its output does not

    _open(tmp_path, runner, [], startup_output=startup_output_opener(store), engine=engine)

    reader = reader_of(store, LOG_OWNER)
    build = reader.list_streams()[0]
    assert build.owner == StartupStep(stage=EnvironmentStartupStage.BUILDING_IMAGE)
    assert b"exporting to image" not in _read_all(reader, build)
    assert warnings
