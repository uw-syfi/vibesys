"""Project GPU contention observations onto the core event stream."""

from __future__ import annotations

from vibesys.events import (
    CoreEventType,
    CoreEventWriter,
    FrameworkSource,
    FrameworkWarningData,
)

__all__ = ["CoreGpuContentionEvents"]


class CoreGpuContentionEvents:
    """Publish GPU contention observations on one run's semantic event stream.

    Implements ``vs_sandbox.gpu_monitor.GpuContentionEvents`` structurally:
    ``vs_sandbox`` names the shape it needs without importing this module.
    """

    def __init__(self, events: CoreEventWriter) -> None:
        """Route contention observations into ``events``."""
        self._events = events

    def contention_detected(self, *, gpu_uuid: str, new_process_count: int, detail: str) -> None:
        """Publish a GPU-contention warning through the framework event sink."""
        self._events.emit(
            CoreEventType.FRAMEWORK_WARNING,
            data=FrameworkWarningData(
                summary=f"GPU contention detected ({new_process_count} new process(es))",
                detail=detail,
                source=FrameworkSource.GPU,
                source_label=gpu_uuid,
                new_process_count=new_process_count,
            ),
        )
