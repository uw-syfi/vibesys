"""A starting run's stages reach the wire journal and the record observer; it is running only once ready."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tests.server.support import build_server_parts

from launch import default_runs
from server.events import EventType, RunStartupProgressData
from server.host_api import RunStatus, StartupFact, StartupProgress, StartupStage
from server.runtime import ServerRuntime
from vibesys.events import (
    CoreEvent,
    CoreEventType,
    EventStatus,
    StartupProgressData,
    make_core_event,
)
from vibesys.events import RunStartupProgressData as CoreRunStartupProgressData
from vs_runtime.api.infrastructure import EnvironmentStartupStage

if TYPE_CHECKING:
    from pathlib import Path

    from server.host_api import FailureReason

_BUILDING = CoreRunStartupProgressData(
    stage=EnvironmentStartupStage.BUILDING_IMAGE,
    progress=StartupProgressData(done=2, total=14, label="RUN npm install -g"),
)
_STARTING_CONTAINER = CoreRunStartupProgressData(stage=EnvironmentStartupStage.STARTING_CONTAINER)


def test_startup_progress_is_journaled_and_the_run_stays_starting_until_ready(
    tmp_path: Path,
) -> None:
    parts = build_server_parts()
    # The server attaches its journal before the run starts; that is not readiness.
    parts.integration.attach(tmp_path, run_id="run-1")
    for data in (_BUILDING, _STARTING_CONTAINER):
        parts.core_events.emit(
            CoreEventType.RUN_STARTUP_PROGRESS, status=EventStatus.ACTIVE, data=data
        )

    assert parts.controller.run_status() is RunStatus.STARTING
    progress = [
        event.data for event in parts.journal.read() if event.type is EventType.RUN_STARTUP_PROGRESS
    ]
    assert progress == [
        RunStartupProgressData(
            stage=StartupStage.BUILDING_IMAGE,
            progress=StartupProgress(done=2, total=14, label="RUN npm install -g"),
        ),
        RunStartupProgressData(stage=StartupStage.STARTING_CONTAINER),
    ]

    parts.controller.ready()

    assert parts.controller.run_status() is RunStatus.RUNNING


def test_the_record_observer_is_told_each_startup_fact(tmp_path: Path) -> None:
    facts: list[object] = []

    class Observer:
        def resolved(self, *, project_root: Path, task: str) -> None:
            del project_root, task

        def listening(self) -> None:
            facts.append("listening")

        def observe(
            self,
            *,
            status: RunStatus | None = None,
            failure: FailureReason | None = None,
            startup: StartupFact | None = None,
        ) -> None:
            facts.append(status or failure or startup)

    runtime = ServerRuntime(
        run_id="run-1",
        runs=default_runs(),
        socket_path=tmp_path / "control.sock",
        detach=True,
        observer=Observer(),
    )
    runtime.controller.attach(tmp_path, run_id="run-1")
    for data in (_BUILDING, _STARTING_CONTAINER):
        runtime.integration.project_event(_startup_event(data))
    runtime.controller.ready()

    assert facts == [
        StartupFact(
            stage=StartupStage.BUILDING_IMAGE,
            progress=StartupProgress(done=2, total=14, label="RUN npm install -g"),
        ),
        StartupFact(stage=StartupStage.STARTING_CONTAINER),
        RunStatus.RUNNING,
    ]


def _startup_event(data: CoreRunStartupProgressData) -> CoreEvent:
    return make_core_event(CoreEventType.RUN_STARTUP_PROGRESS, status=EventStatus.ACTIVE, data=data)


def test_a_pause_resumed_during_the_image_build_keeps_the_run_starting(tmp_path: Path) -> None:
    """Regression: the record lost the build's progress and read ``running`` before ready."""
    facts: list[object] = []

    class Observer:
        def resolved(self, *, project_root: Path, task: str) -> None:
            del project_root, task

        def listening(self) -> None:
            facts.append("listening")

        def observe(
            self,
            *,
            status: RunStatus | None = None,
            failure: FailureReason | None = None,
            startup: StartupFact | None = None,
        ) -> None:
            facts.append(status or failure or startup)

    runtime = ServerRuntime(
        run_id="run-1",
        runs=default_runs(),
        socket_path=tmp_path / "control.sock",
        detach=True,
        observer=Observer(),
    )
    runtime.controller.attach(tmp_path, run_id="run-1")
    runtime.integration.project_event(_startup_event(_BUILDING))
    runtime.controller.pause_after_call()
    runtime.controller.resume()
    runtime.controller.pause_after_call()
    runtime.integration.project_event(_startup_event(_STARTING_CONTAINER))

    assert runtime.controller.run_status() is RunStatus.STARTING
    runtime.controller.ready()

    assert runtime.controller.run_status() is RunStatus.PAUSING
    assert facts == [
        StartupFact(
            stage=StartupStage.BUILDING_IMAGE,
            progress=StartupProgress(done=2, total=14, label="RUN npm install -g"),
        ),
        StartupFact(stage=StartupStage.STARTING_CONTAINER),
        RunStatus.PAUSING,
    ]
