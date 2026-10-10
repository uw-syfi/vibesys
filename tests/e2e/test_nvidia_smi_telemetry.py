"""``NvidiaSmiTelemetry`` against a stand-in ``nvidia-smi`` executable: the contract the Fake also passes.

The unit tests in ``libs/vs-sandbox/tests/test_gpu_monitor.py`` run the monitor on a scripted
telemetry source and a virtual clock. What only a real child process shows is here: the
command line the adapter runs, its parsing of real output, and its answer when the
executable is missing or exits with an error.
"""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api import GpuInfo, NvidiaSmiTelemetry
from vs_sandbox.api.testing import GpuTelemetryContract, TelemetryHarness

if TYPE_CHECKING:
    from pathlib import Path


def _write_executable(path: Path, body: str) -> str:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class TestNvidiaSmiTelemetry(GpuTelemetryContract):
    @pytest.fixture(autouse=True)
    def _directory(self, tmp_path: Path) -> None:
        self._dir = tmp_path

    def _reporting(self, gpus: list[GpuInfo], processes: str) -> NvidiaSmiTelemetry:
        rows = "".join(
            f"{g.index}, {g.uuid}, {g.name}, {g.memory_used_mib}, {g.memory_total_mib}, "
            f"{g.utilization_pct}\n"
            for g in gpus
        )
        (self._dir / "gpus.csv").write_text(rows)
        (self._dir / "procs.csv").write_text(processes)
        script = _write_executable(
            self._dir / "nvidia-smi",
            f'case "$1" in --query-gpu=*) cat "{self._dir}/gpus.csv";; '
            f'--query-compute-apps=*) cat "{self._dir}/procs.csv";; *) exit 2;; esac',
        )
        return NvidiaSmiTelemetry(script)

    def harness(self) -> TelemetryHarness:
        return TelemetryHarness(
            reporting=self._reporting,
            without_driver=lambda: NvidiaSmiTelemetry(str(self._dir / "missing-nvidia-smi")),
            failing=lambda: NvidiaSmiTelemetry(
                _write_executable(self._dir / "broken-smi", "echo oops; exit 9")
            ),
        )
