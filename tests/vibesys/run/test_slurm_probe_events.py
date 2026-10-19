"""`CoreWorkspaceRootProbeEvents` publishes the workspace-root probe's reports as events."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from vibesys.events import CoreEventType, EventStatus, WorkspaceRootProbeData
from vibesys.run import EventJournal
from vibesys.run.slurm_probe_events import CoreWorkspaceRootProbeEvents
from vs_sim.api.testing import ManualClock, SimThreads
from vs_slurm.api import (
    WorkspaceRootProbeFailed,
    WorkspaceRootProbeProven,
    WorkspaceRootProbeQueued,
)

if TYPE_CHECKING:
    from pathlib import Path


def test_each_probe_report_is_one_event_with_its_status(tmp_path: Path) -> None:
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    journal.attach(tmp_path / "run-1", "run-1")
    events = CoreWorkspaceRootProbeEvents(journal)

    events.report(
        WorkspaceRootProbeQueued(
            job_id="5000", pending_reason=None, waited_seconds=0.0, queue_timeout_seconds=1800.0
        )
    )
    events.report(WorkspaceRootProbeProven())
    events.report(WorkspaceRootProbeFailed(failure="probe job 5000 was never scheduled"))

    queued, proven, failed = journal.read()
    assert {event.type for event in (queued, proven, failed)} == {
        CoreEventType.WORKSPACE_ROOT_PROBE
    }
    assert (queued.status, proven.status, failed.status) == (
        EventStatus.ACTIVE,
        EventStatus.COMPLETED,
        EventStatus.FAILED,
    )
    assert queued.data == WorkspaceRootProbeData(
        state="queued", job_id="5000", waited_seconds=0.0, queue_timeout_seconds=1800.0
    )
    assert proven.data == WorkspaceRootProbeData(state="proven")
    assert failed.data == WorkspaceRootProbeData(
        state="failed", failure="probe job 5000 was never scheduled"
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"state": "queued"},
        {"state": "queued", "job_id": "5000"},
        {"state": "proven", "job_id": "5000", "waited_seconds": 1.0},
        {"state": "failed"},
        {"state": "proven", "failure": "stale"},
        {"state": "queued", "job_id": "5000", "waited_seconds": -1.0},
    ],
)
def test_a_payload_whose_fields_contradict_its_state_is_rejected(
    fields: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        WorkspaceRootProbeData.model_validate(fields)
