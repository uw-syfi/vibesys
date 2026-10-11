"""Slurm runners stop every local process that can retain their merged output pipe."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    AgentGpuLauncher,
    GateKind,
    GpuCommand,
    GpuJobRequest,
    SlurmCommandGateRunner,
)
from vs_sim.api import ProbeResult
from vs_sim.api.testing import FakeMergedOutputProcessLauncher, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_sim.api import Event


class _DescendantHeldProcess:
    """A leader that exits on SIGTERM while some descendants retain its pipe."""

    def __init__(
        self,
        threads: SimThreads,
        *,
        term_survivors: int,
        trace: list[str],
        on_term: Callable[[], None] | None = None,
    ) -> None:
        self._output_closed = threads.event()
        self._term_survivors = term_survivors
        self._trace = trace
        self._on_term = on_term
        self._announced = False
        self._status = 143

    def read(self, max_bytes: int) -> bytes:
        assert max_bytes > 0
        if not self._announced:
            self._announced = True
            return b"wrapper ready\n"
        assert self._output_closed.wait(timeout=300)
        return b""

    def wait(self) -> int:
        assert self._output_closed.is_set()
        return self._status

    def settle(self, status: int) -> None:
        """Finish naturally while another simulated thread is in ``scancel``."""
        self._status = status
        self._output_closed.set()

    def terminate_group(self) -> None:
        self._trace.append("SIGTERM")
        if self._on_term is not None:
            self._on_term()
        if self._term_survivors == 0:
            self._output_closed.set()

    def kill_group(self) -> None:
        self._trace.append("SIGKILL")
        self._output_closed.set()


class _ScancelProbe:
    def __init__(self, trace: list[str], on_scancel: Callable[[], None] | None = None) -> None:
        self._trace = trace
        self._on_scancel = on_scancel

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProbeResult:
        assert timeout_seconds > 0
        assert "scancel" in argv[-1]
        self._trace.append("scancel")
        if self._on_scancel is not None:
            self._on_scancel()
        return ProbeResult(0, "")


def _cancel_on_ready(cancel: Event, output: bytearray, chunk: bytes) -> None:
    output.extend(chunk)
    if chunk == b"wrapper ready\n":
        cancel.set()


@settings(max_examples=10)
@example(term_survivors=0)
@example(term_survivors=1)
@given(term_survivors=st.integers(min_value=0, max_value=8))
def test_agent_gpu_cancellation_scancels_before_stopping_the_whole_local_group(
    term_survivors: int,
) -> None:
    """Any descendant that survives SIGTERM is killed after the virtual grace period."""
    threads = SimThreads()
    cancel = threads.event()
    trace: list[str] = []
    process = _DescendantHeldProcess(
        threads,
        term_survivors=term_survivors,
        trace=trace,
    )
    processes = FakeMergedOutputProcessLauncher(
        lambda _argv, _cwd, _env: process,
    )
    launcher = AgentGpuLauncher(
        AgentGpuConfig(
            partitions=("gpu",),
            max_gpus=1,
            max_time_minutes=5,
            default_time_minutes=5,
        ),
        ("transport", "shell"),
        probe=_ScancelProbe(trace),
        processes=processes,
        threads=threads,
    )
    output = bytearray()

    status = threads.run(
        lambda: launcher.run(
            GpuJobRequest(gpus=1, time_minutes=5),
            GpuCommand(argv=("work",), cwd=Path("workspace"), env={"TOKEN": "value"}),
            write=lambda chunk: _cancel_on_ready(cancel, output, chunk),
            cancel=cancel,
        )
    )

    expected = ["scancel", "SIGTERM"]
    if term_survivors:
        expected.append("SIGKILL")
    assert trace == expected
    assert status == 143
    assert output.endswith(b"wrapper ready\n")
    assert processes.started[0][2] == {"TOKEN": "value"}


def test_agent_gpu_does_not_signal_a_group_that_settles_during_scancel() -> None:
    """Remote cleanup can outlast the local leader, whose PGID may then be reused."""
    # This seed runs the output-draining thread while the cancellation worker
    # is inside the scripted scancel call.
    threads = SimThreads(schedule_seed=22)
    cancel = threads.event()
    trace: list[str] = []
    process = _DescendantHeldProcess(threads, term_survivors=0, trace=trace)
    launcher = AgentGpuLauncher(
        AgentGpuConfig(
            partitions=("gpu",),
            max_gpus=1,
            max_time_minutes=5,
            default_time_minutes=5,
        ),
        ("transport", "shell"),
        probe=_ScancelProbe(trace, on_scancel=lambda: process.settle(0)),
        processes=FakeMergedOutputProcessLauncher(lambda _argv, _cwd, _env: process),
        threads=threads,
    )

    status = threads.run(
        lambda: launcher.run(
            GpuJobRequest(gpus=1, time_minutes=5),
            GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
            write=lambda chunk: _cancel_on_ready(cancel, bytearray(), chunk),
            cancel=cancel,
        )
    )

    assert status == 0
    assert trace == ["scancel"]


def test_gate_cancellation_kills_descendants_only_after_the_wrapper_scancels() -> None:
    """The gate wrapper handles SIGTERM first; its pipe-holding descendant cannot hang run()."""
    threads = SimThreads()
    cancel = threads.event()
    trace: list[str] = []
    process = _DescendantHeldProcess(
        threads,
        term_survivors=1,
        trace=trace,
        on_term=lambda: trace.append("wrapper scancel"),
    )
    processes = FakeMergedOutputProcessLauncher(
        lambda _argv, _cwd, _env: process,
    )
    runner = SlurmCommandGateRunner(
        Path("plan.json"),
        env={"CLUSTER": "test"},
        wrapper=("gate-wrapper",),
        processes=processes,
        threads=threads,
    )
    output = bytearray()

    status = threads.run(
        lambda: runner.run(
            GateKind.ACCURACY,
            ("--case", "one"),
            cwd=Path("workspace"),
            write=lambda chunk: _cancel_on_ready(cancel, output, chunk),
            cancel=cancel,
        )
    )

    assert trace == ["SIGTERM", "wrapper scancel", "SIGKILL"]
    assert status == 143
    assert output == b"wrapper ready\n"
    assert processes.started == [
        (
            ("gate-wrapper", "--plan", "plan.json", "accuracy", "--case", "one"),
            Path("workspace"),
            {"CLUSTER": "test"},
        )
    ]
