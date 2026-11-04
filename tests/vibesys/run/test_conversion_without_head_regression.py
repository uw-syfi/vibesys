"""A complete retained log without its optional head remains repairable."""

from pathlib import Path

import pytest

from vibesys.api.transport import ConversionStatus, convert_run
from vibesys.events import CoreEventType, RunStartedData
from vibesys.run import EventJournal
from vs_project.api import Project
from vs_sim.api.testing import ManualClock, SimThreads


def test_conversion_repairs_a_missing_head_before_inspecting_its_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Handoff to fix G: its interrupt probe must allow a missing projection."""
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "home"))
    root = tmp_path / "project"
    root.mkdir()
    project = Project.open(root)
    journal = EventJournal(ManualClock(), SimThreads())
    journal.attach(Project.log_directory_for(root, "run-a"), "run-a")
    journal.emit(
        CoreEventType.RUN_STARTED,
        data=RunStartedData(outer_loop="single-agent", input="input"),
    )
    journal.emit(CoreEventType.RUN_FINISHED)
    convert_run(project, "run-a", clock=ManualClock(), threads=SimThreads())
    head = Project.run_head_path_for(root, "run-a")
    head.unlink()
    result = convert_run(project, "run-a", clock=ManualClock(), threads=SimThreads())
    assert result.status is ConversionStatus.CONVERTED
    assert head.is_file()
