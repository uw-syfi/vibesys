"""Public behavior tests for GPU contention event translation."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import CoreEventType, GpuContentionData
from vibesys.run import EventJournal
from vibesys.run.gpu_events import CoreGpuContentionEvents
from vs_sandbox.api import ContentionEventSink, ContentionStatus, GpuInfo

if TYPE_CHECKING:
    from pathlib import Path


def _attached(tmp_path: Path, run_id: str) -> tuple[EventJournal, CoreGpuContentionEvents]:
    journal = EventJournal()
    journal.attach(tmp_path / run_id, run_id)
    return journal, CoreGpuContentionEvents(journal)


def test_adapter_implements_the_public_contention_event_sink(tmp_path: Path) -> None:
    _, publisher = _attached(tmp_path, "run-1")
    public_sink: ContentionEventSink = publisher

    assert isinstance(public_sink, ContentionEventSink)


def test_contention_starting_publishes_the_device_snapshot(tmp_path: Path) -> None:
    journal, publisher = _attached(tmp_path, "run-1")
    status = ContentionStatus(
        is_contended=True,
        new_procs=[{"pid": 123}, {"pid": 456}],
        gpu=GpuInfo(
            index=2,
            uuid="GPU-aaaa",
            name="H100",
            memory_used_mib=4096,
            memory_total_mib=81559,
            utilization_pct=30,
        ),
        timestamp="2026-01-01T00:00:00Z",
    )

    publisher.contention_changed(status)

    events = journal.read()
    assert [event.type for event in events] == [CoreEventType.GPU_CONTENTION_CHANGED]
    assert events[0].data == GpuContentionData(
        is_contended=True,
        new_process_count=2,
        gpu_index=2,
        memory_used_mib=4096,
        memory_total_mib=81559,
        utilization_pct=30,
    )


def test_contention_clearing_with_no_device_reading_publishes_unknown_device_fields(
    tmp_path: Path,
) -> None:
    journal, publisher = _attached(tmp_path, "run-1")
    status = ContentionStatus(is_contended=False, new_procs=[], gpu=None, timestamp="")

    publisher.contention_changed(status)

    event = journal.read()[0]
    assert event.data == GpuContentionData(
        is_contended=False,
        new_process_count=0,
        gpu_index=None,
        memory_used_mib=None,
        memory_total_mib=None,
        utilization_pct=None,
    )
