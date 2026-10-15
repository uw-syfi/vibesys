"""Real-registry proof that a detached run's record shows its startup stages, and why a build failed.

A detached server is composed in process around a fake run that reports its
startup the way the run environment does (core ``run_startup_progress``
events), with the production ``RecordPublisher`` writing a real registry on
disk. A second reader lists that registry, as ``vibesys instances list`` does,
while the run is starting, and after the server exits.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support.real_git import NO_GITHUB, REAL_GIT

from entrypoints.detached_run import RecordPublisher
from entrypoints.production import production_runs
from server.host_api import (
    InstanceStatus,
    LiveInstanceRecord,
    RunPhase,
    RunStatus,
    StartupProgress,
    StartupStage,
)
from server.registry import (
    FileInstanceMetadataStore,
    FileInstanceStore,
    LiveRegistry,
    host_facts,
    instance_root,
    instance_socket_path,
    new_instance_id,
)
from server.runtime import ServerRuntime
from vibesys.events import (
    CoreEventType,
    EventStatus,
    RunStartupProgressData,
    StartupProgressData,
)
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api.images import TaskImageBuildError
from vs_runtime.api.infrastructure import EnvironmentStartupStage, EnvironmentSystem
from vs_sim.api import (
    OsProcesses,
    OsThreads,
    Platform,
    SubprocessPipedProcessLauncher,
    SystemClock,
    UnixNetwork,
)
from vs_sim.api.testing import HANG_GUARD_S, NO_PROCESSES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

_BUILD_FAILURE = "Could not build agent image (exit 1): npm ERR! network"


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # A short runtime directory under `/tmp`: pytest's temporary paths and
    # macOS's `$TMPDIR` can push a socket path past the 103-byte Unix limit.
    runtime = Path(tempfile.mkdtemp(prefix="vs-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    try:
        yield instance_root(os.environ, os.getuid())
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def _wait_until(condition: Callable[[], bool], threads: OsThreads) -> None:
    """Poll a real registry until ``condition`` holds; the guard only bounds a hang."""
    deadline = threads.now() + HANG_GUARD_S
    while not condition():
        assert threads.now() < deadline, "the record never reached the expected phase"
        threads.sleep(0.02)


def _serve(
    root: Path, run: Callable[[LocalRunIntegration, ServerRuntime], None]
) -> tuple[str, LiveRegistry, threading.Thread, list[BaseException]]:
    instance_id = new_instance_id()
    socket_path = instance_socket_path(root, instance_id)
    socket_path.parent.mkdir(parents=True, mode=0o700)
    clock = SystemClock()
    registry = LiveRegistry(FileInstanceStore(root), clock)
    errors: list[BaseException] = []

    def serve() -> None:
        hostname, version = host_facts()
        integration = LocalRunIntegration(
            clock=clock,
            git=REAL_GIT,
            github=NO_GITHUB,
            threads=OsThreads(),
            system=_real_system(),
        )
        try:
            with registry.register(instance_id) as hold:
                publisher = RecordPublisher(
                    hold,
                    LiveInstanceRecord(
                        id=instance_id,
                        status=InstanceStatus.STARTING,
                        phase=RunPhase(status=RunStatus.STARTING, stage=StartupStage.RESOLVING),
                        log_path=str(root / "server.log"),
                        socket_path=str(socket_path),
                        project_root=str(root),
                        run_id="run-1",
                        pid=os.getpid(),
                        started_at=clock.now(),
                        hostname=hostname,
                        vibesys_version=version,
                    ),
                    clock,
                    metadata=FileInstanceMetadataStore(root),
                    threads=OsThreads(),
                )
                runtime = ServerRuntime(
                    run_id="run-1",
                    runs=production_runs(),
                    socket_path=socket_path,
                    detach=True,
                    observer=publisher,
                    platform=Platform.real(),
                )
                integration.events.subscribe(runtime.integration.project_event)
                try:
                    runtime.run(lambda: run(integration, runtime))
                finally:
                    publisher.end()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-186103 [BLE001]; the server thread hands its failure to the test thread, which asserts on it; re-raising in a thread would only print it.
            errors.append(error)
        finally:
            integration.close()

    server = threading.Thread(target=serve)
    server.start()
    return instance_id, registry, server, errors


def _report(integration: LocalRunIntegration, data: RunStartupProgressData) -> None:
    integration.events.emit(
        CoreEventType.RUN_STARTUP_PROGRESS, status=EventStatus.ACTIVE, data=data
    )


def _phase(registry: LiveRegistry, instance_id: str) -> RunPhase | None:
    record = registry.find(instance_id)
    return None if record is None else record.phase


def test_a_starting_run_shows_its_build_progress_then_runs_once_ready(root: Path) -> None:
    threads = OsThreads()
    seen = threading.Event()
    building = RunPhase(
        status=RunStatus.STARTING,
        stage=StartupStage.BUILDING_IMAGE,
        progress=StartupProgress(done=2, total=3, label="RUN pip install uv"),
    )

    def run(integration: LocalRunIntegration, runtime: ServerRuntime) -> None:
        _report(integration, RunStartupProgressData(stage=EnvironmentStartupStage.BUILDING_IMAGE))
        for done, label in enumerate(("FROM python", "RUN npm install -g", "RUN pip install uv")):
            _report(
                integration,
                RunStartupProgressData(
                    stage=EnvironmentStartupStage.BUILDING_IMAGE,
                    progress=StartupProgressData(done=done, total=3, label=label),
                ),
            )
        assert seen.wait(HANG_GUARD_S)
        _report(
            integration, RunStartupProgressData(stage=EnvironmentStartupStage.STARTING_CONTAINER)
        )
        runtime.controller.ready()

    instance_id, registry, server, errors = _serve(root, run)
    try:
        _wait_until(lambda: _phase(registry, instance_id) == building, threads)
        seen.set()
        server.join(HANG_GUARD_S)
    finally:
        seen.set()
        server.join(HANG_GUARD_S)

    assert not server.is_alive()
    assert errors == []
    (ended,) = registry.list().ended
    assert ended.phase == RunPhase(status=RunStatus.COMPLETED)


def test_a_failed_image_build_leaves_a_tombstone_naming_the_stage_and_reason(root: Path) -> None:
    threads = OsThreads()

    def run(integration: LocalRunIntegration, runtime: ServerRuntime) -> None:
        del runtime
        _report(
            integration,
            RunStartupProgressData(
                stage=EnvironmentStartupStage.BUILDING_IMAGE,
                progress=StartupProgressData(done=1, total=3, label="RUN npm install -g"),
            ),
        )
        error = TaskImageBuildError(_BUILD_FAILURE)
        # What a run session reports when its run raises, before re-raising.
        integration.events.emit(
            CoreEventType.RUN_FAILED,
            f"{type(error).__name__}: {error}",
            status=EventStatus.FAILED,
        )
        raise error

    instance_id, registry, server, errors = _serve(root, run)
    server.join(HANG_GUARD_S)

    assert not server.is_alive()
    assert [type(error) for error in errors] == [TaskImageBuildError]
    _wait_until(lambda: registry.find(instance_id) is None, threads)
    (ended,) = registry.list().ended
    assert ended.phase.status is RunStatus.FAILED
    assert ended.phase.reason is not None
    assert ended.phase.reason.stage == "building_image"
    assert _BUILD_FAILURE in ended.phase.reason.message


def _real_system() -> EnvironmentSystem:
    """The host's own threads, sockets and processes; this test runs no Slurm program."""
    return EnvironmentSystem(
        threads=OsThreads(),
        network=UnixNetwork(),
        processes=OsProcesses(),
        launcher=SubprocessPipedProcessLauncher(),
        slurm_processes=NO_PROCESSES,
    )
