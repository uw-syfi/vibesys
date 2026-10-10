"""Contract suite for :class:`~vs_sandbox.gpu_monitor.GpuTelemetry`.

Subclass :class:`GpuTelemetryContract`, name the subclass ``Test<Variant>`` and implement
:meth:`~GpuTelemetryContract.harness`. The real ``NvidiaSmiTelemetry`` (against a stand-in
executable, in ``tests/e2e``) and ``FakeGpuTelemetry`` pass the same cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sandbox.gpu_monitor import GpuInfo, parse_gpu_process_output

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sandbox.gpu_monitor import GpuTelemetry

_GPUS = [
    GpuInfo(0, "GPU-aaaa", "H100", 5000, 81559, 30),
    GpuInfo(1, "GPU-bbbb", "H100", 100, 81559, 0),
]
_PROCESSES = "1000, python, 4096, GPU-aaaa\n2000, train.py, 8192, GPU-bbbb\n"


@dataclass(frozen=True)
class TelemetryHarness:
    """How one implementation is put in each situation the contract describes."""

    reporting: Callable[[list[GpuInfo], str], GpuTelemetry]
    """A source that reports these GPUs and this compute-apps CSV."""
    without_driver: Callable[[], GpuTelemetry]
    """A source on a host with no GPU driver."""
    failing: Callable[[], GpuTelemetry]
    """A source whose driver reports an error."""


class GpuTelemetryContract:
    """Cases for :class:`~vs_sandbox.gpu_monitor.GpuTelemetry`."""

    def harness(self) -> TelemetryHarness:
        """A fresh harness for one case."""
        raise NotImplementedError

    def test_reports_the_gpus(self) -> None:
        """Every GPU the driver lists is reported with its memory and utilisation."""
        assert self.harness().reporting(_GPUS, _PROCESSES).gpus() == _GPUS

    def test_reports_the_compute_processes(self) -> None:
        """The compute-apps rows come back in a form the process parser reads."""
        raw = self.harness().reporting(_GPUS, _PROCESSES).compute_processes()
        assert [(p["pid"], p["gpu_uuid"]) for p in parse_gpu_process_output(raw)] == [
            (1000, "GPU-aaaa"),
            (2000, "GPU-bbbb"),
        ]

    def test_a_host_without_a_driver_reports_nothing(self) -> None:
        """No driver is an empty answer, not an error."""
        telemetry = self.harness().without_driver()
        assert telemetry.gpus() == []
        assert telemetry.compute_processes() == ""

    def test_a_driver_error_reports_nothing(self) -> None:
        """A driver that exits with an error is an empty answer, not an error."""
        telemetry = self.harness().failing()
        assert telemetry.gpus() == []
        assert telemetry.compute_processes() == ""
