"""Project GPU contention monitor observations onto the core event stream."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import CoreEventType, CoreEventWriter, GpuContentionData

if TYPE_CHECKING:
    from vs_sandbox.api import ContentionStatus

__all__ = ["CoreGpuContentionEvents"]


class CoreGpuContentionEvents:
    """Publish GPU contention transitions on one run's semantic event stream.

    Implements ``vs_sandbox.api.ContentionEventSink``: ``GpuContentionMonitor``
    calls ``contention_changed`` only when ``is_contended`` flips, so each
    call here becomes exactly one published event.
    """

    def __init__(self, events: CoreEventWriter) -> None:
        """Route contention transitions into ``events``."""
        self._events = events

    def contention_changed(self, status: ContentionStatus) -> None:
        """Publish one contention-state transition."""
        gpu = status.gpu
        self._events.emit(
            CoreEventType.GPU_CONTENTION_CHANGED,
            data=GpuContentionData(
                is_contended=status.is_contended,
                new_process_count=len(status.new_procs),
                gpu_index=gpu.index if gpu is not None else None,
                memory_used_mib=gpu.memory_used_mib if gpu is not None else None,
                memory_total_mib=gpu.memory_total_mib if gpu is not None else None,
                utilization_pct=gpu.utilization_pct if gpu is not None else None,
            ),
        )
