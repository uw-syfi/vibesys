"""The agent GPU launcher stops every local process that can retain their merged output pipe."""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import example, given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    AgentGpuLauncher,
    GpuCommand,
    GpuJobRequest,
)
from vs_sim.api import ProbeResult
from vs_sim.api.testing import FakePipedProcessLauncher, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_sim.api import Event


class _HeldOutput(io.TextIOBase):
    """Merged output that announces readiness, then stays open until the group settles."""

    def __init__(self, closed: Event) -> None:
        super().__init__()
        self._output_closed = closed
        self._announced = False

    def read(self, size: int | None = -1, /) -> str:
        assert size is not None
        assert size > 0
        if not self._announced:
            self._announced = True
            return "wrapper ready\n"
        assert self._output_closed.wait(timeout=300)
        return ""


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
        self._status = 143
        self.stdout: io.TextIOBase = _HeldOutput(self._output_closed)
        self.stderr: io.TextIOBase = io.StringIO()

    def wait(self, timeout: float | None) -> int:
        assert timeout is None
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

    def cancel_output(self) -> None:
        """The launcher stops output by stopping the group, never by cancelling reads."""
        raise AssertionError


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


@settings(max_examples=examples(pr=5, nightly=10))
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
    processes = FakePipedProcessLauncher(
        lambda _start: process,
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
    assert processes.started[0].env == {"TOKEN": "value"}
    assert processes.started[0].merge_stderr


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
        processes=FakePipedProcessLauncher(lambda _start: process),
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
