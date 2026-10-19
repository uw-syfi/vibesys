"""`CoreWorkspaceRootProbeEvents` publishes the workspace-root probe's reports as events."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.events import CoreEventType, EventStatus, WorkspaceRootProbeData
from vibesys.run import EventJournal
from vibesys.run.environment_failure import RunEnvironmentFailure
from vibesys.run.slurm_probe_events import CoreWorkspaceRootProbeEvents
from vs_core.api import StopMode
from vs_runtime.api.infrastructure import create_run_control_channel
from vs_sim.api.testing import ManualClock, SimThreads
from vs_slurm.api import (
    WorkspaceRootProbeFailed,
    WorkspaceRootProbeProven,
    WorkspaceRootProbeQueued,
    WorkspaceRootProbeRetrying,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import RunControlChannel


def _environment(threads: SimThreads) -> tuple[RunEnvironmentFailure, RunControlChannel]:
    control = create_run_control_channel(lambda _transition: None, threads=threads)
    return RunEnvironmentFailure(control), control


def test_each_probe_report_is_one_event_with_its_status(tmp_path: Path) -> None:
    threads = SimThreads()
    journal = EventJournal(clock=ManualClock(), threads=threads)
    journal.attach(tmp_path / "run-1", "run-1")
    environment, _ = _environment(threads)
    events = CoreWorkspaceRootProbeEvents(journal, environment)

    events.report(
        WorkspaceRootProbeQueued(
            job_id="5000", pending_reason=None, waited_seconds=0.0, queue_timeout_seconds=1800.0
        )
    )
    events.report(
        WorkspaceRootProbeRetrying(
            failure="probe job 5000 was never scheduled", attempt=2, retry_in_seconds=60.0
        )
    )
    events.report(WorkspaceRootProbeProven())
    events.report(WorkspaceRootProbeFailed(failure="probe job 5001 exited with code 1"))

    queued, retrying, proven, failed = journal.read()
    assert {event.type for event in (queued, retrying, proven, failed)} == {
        CoreEventType.WORKSPACE_ROOT_PROBE
    }
    assert (queued.status, retrying.status, proven.status, failed.status) == (
        EventStatus.ACTIVE,
        EventStatus.ACTIVE,
        EventStatus.COMPLETED,
        EventStatus.FAILED,
    )
    assert queued.data == WorkspaceRootProbeData(
        state="queued", job_id="5000", waited_seconds=0.0, queue_timeout_seconds=1800.0
    )
    assert retrying.data == WorkspaceRootProbeData(
        state="retrying",
        failure="probe job 5000 was never scheduled",
        attempt=2,
        retry_in_seconds=60.0,
    )
    assert proven.data == WorkspaceRootProbeData(state="proven")
    assert failed.data == WorkspaceRootProbeData(
        state="failed", failure="probe job 5001 exited with code 1"
    )


def test_only_a_failed_probe_ends_the_run_and_it_names_the_probe(tmp_path: Path) -> None:
    threads = SimThreads()
    journal = EventJournal(clock=ManualClock(), threads=threads)
    journal.attach(tmp_path / "run-1", "run-1")
    environment, control = _environment(threads)
    events = CoreWorkspaceRootProbeEvents(journal, environment)

    events.report(
        WorkspaceRootProbeRetrying(failure="never scheduled", attempt=2, retry_in_seconds=60.0)
    )
    assert environment.reason is None
    assert control.stop_mode() is None

    events.report(WorkspaceRootProbeFailed(failure="probe job 5001 exited with code 1"))
    events.report(WorkspaceRootProbeFailed(failure="a later failure changes nothing"))

    assert environment.reason == "probe job 5001 exited with code 1"
    assert control.stop_mode() is StopMode.CANCEL


@pytest.mark.parametrize(
    "fields",
    [
        {"state": "queued"},
        {"state": "queued", "job_id": "5000"},
        {"state": "proven", "job_id": "5000", "waited_seconds": 1.0},
        {"state": "failed"},
        {"state": "proven", "failure": "stale"},
        {"state": "retrying", "failure": "never scheduled"},
        {"state": "retrying", "attempt": 2, "retry_in_seconds": 60.0},
        {"state": "failed", "failure": "x", "attempt": 2, "retry_in_seconds": 60.0},
        {"state": "retrying", "failure": "x", "attempt": 1, "retry_in_seconds": 60.0},
        {"state": "queued", "job_id": "5000", "waited_seconds": -1.0},
    ],
)
def test_a_payload_whose_fields_contradict_its_state_is_rejected(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WorkspaceRootProbeData.model_validate(fields)
