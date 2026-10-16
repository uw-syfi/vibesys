"""An operator's force stop is kept in the run's state, for a later resume to honor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_project.api.testing import run_execution_record

if TYPE_CHECKING:
    from pathlib import Path


def _project_with_run(root: Path) -> tuple[Project, str]:
    project = Project.open(root)
    project.state.create_project("stop", now=datetime(2026, 1, 1, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "stop",
        branch="test",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="dynamic", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


def test_a_recorded_stop_is_seen_by_a_later_reader_of_the_run(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)
    assert not project.state.stop_requested(run_id)

    project.state.record_stop_request(run_id)

    assert Project.open(tmp_path).state.stop_requested(run_id)


def test_recording_a_stop_twice_is_one_stop(tmp_path: Path) -> None:
    project, run_id = _project_with_run(tmp_path)

    project.state.record_stop_request(run_id)
    project.state.record_stop_request(run_id)

    assert project.state.stop_requested(run_id)
