"""`CoreGpuContentionEvents` publishes GPU contention onto the semantic event stream."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import CoreEventType, FrameworkSource, FrameworkWarningData
from vibesys.run import EventJournal
from vibesys.run.gpu_events import CoreGpuContentionEvents
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from pathlib import Path


def _journal_and_events(
    tmp_path: Path, run_id: str = "run-1"
) -> tuple[EventJournal, CoreGpuContentionEvents]:
    journal = EventJournal(clock=ManualClock(), threads=SimThreads())
    journal.attach(tmp_path / run_id, run_id)
    return journal, CoreGpuContentionEvents(journal)


def test_a_contention_observation_is_published_as_a_gpu_framework_warning(
    tmp_path: Path,
) -> None:
    journal, events = _journal_and_events(tmp_path)

    events.contention_detected(
        gpu_uuid="GPU-1234",
        new_process_count=2,
        detail="2 new process(es) on GPU 0",
    )

    (event,) = journal.read()
    assert event.type is CoreEventType.FRAMEWORK_WARNING
    assert event.data == FrameworkWarningData(
        summary="GPU contention detected (2 new process(es))",
        detail="2 new process(es) on GPU 0",
        source=FrameworkSource.GPU,
        source_label="GPU-1234",
        new_process_count=2,
    )


def test_two_observations_on_one_journal_are_recorded_independently(tmp_path: Path) -> None:
    journal, events = _journal_and_events(tmp_path)

    events.contention_detected(gpu_uuid="GPU-1", new_process_count=1, detail="one")
    events.contention_detected(gpu_uuid="GPU-2", new_process_count=3, detail="three")

    first, second = journal.read()
    assert isinstance(first.data, FrameworkWarningData)
    assert first.data.source_label == "GPU-1"
    assert isinstance(second.data, FrameworkWarningData)
    assert second.data.source_label == "GPU-2"
