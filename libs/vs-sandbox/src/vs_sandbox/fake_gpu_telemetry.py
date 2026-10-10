"""An in-memory :class:`~vs_sandbox.gpu_monitor.GpuTelemetry` a test sets and changes."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vs_sandbox.gpu_monitor import GpuInfo


class FakeGpuTelemetry:
    """GPU state held in attributes: set them between reads to script what the monitor sees.

    ``processes`` is the raw compute-apps CSV; ``fail_with``, when set, makes every read raise it
    (a driver that errors instead of reporting nothing). ``process_reads`` counts
    ``compute_processes`` calls.
    """

    def __init__(self, gpus: list[GpuInfo] | None = None, processes: str = "") -> None:
        """Start reporting *gpus* and the raw *processes* CSV."""
        self.gpu_list: list[GpuInfo] = list(gpus or [])
        self.processes = processes
        self.fail_with: Exception | None = None
        self.process_reads = 0

    def gpus(self) -> list[GpuInfo]:
        """The scripted GPUs."""
        if self.fail_with is not None:
            raise self.fail_with
        return list(self.gpu_list)

    def compute_processes(self) -> str:
        """The scripted compute-apps CSV."""
        self.process_reads += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self.processes
