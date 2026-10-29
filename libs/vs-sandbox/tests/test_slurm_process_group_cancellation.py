"""The agent GPU launcher cancels the job, stops every local process holding its output, and checks.

``scancel`` and the ``squeue`` check after it run through the transport's shell,
which may be a ``docker exec`` or ``ssh`` client; stopping that client does not
stop the job, so only the scheduler's own answers decide whether a cancel took.
"""

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


class _SlurmProbe:
    """Answers ``scancel`` and ``squeue`` in turn from scripts; the last answer repeats."""

    def __init__(
        self,
        trace: list[str],
        on_scancel: Callable[[], None] | None = None,
        *,
        scancel: Sequence[ProbeResult | None] = (ProbeResult(0, ""),),
        squeue: Sequence[ProbeResult | None] = (ProbeResult(0, ""),),
    ) -> None:
        self._trace = trace
        self._on_scancel = on_scancel
        self._answers = {"scancel": list(scancel), "squeue": list(squeue)}

    def run(self, argv: Sequence[str], *, timeout_seconds: float) -> ProbeResult | None:
        assert timeout_seconds > 0
        program = argv[-1].split()[0]
        if program == "sinfo":  # the partition-limit query before submission
            return ProbeResult(0, "gpu* 2:00:00\n")
        self._trace.append(program)
        if program == "scancel" and self._on_scancel is not None:
            self._on_scancel()
        answers = self._answers[program]
        return answers.pop(0) if len(answers) > 1 else answers[0]


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
        probe=_SlurmProbe(trace),
        processes=processes,
        threads=threads,
    )
    output = bytearray()

    outcome = threads.run(
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
    assert trace == [*expected, *(["squeue"] * 6)]
    assert outcome.exit_code == 143
    assert outcome.termination_confirmed
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
        probe=_SlurmProbe(trace, on_scancel=lambda: process.settle(0)),
        processes=FakePipedProcessLauncher(lambda _start: process),
        threads=threads,
    )

    outcome = threads.run(
        lambda: launcher.run(
            GpuJobRequest(gpus=1, time_minutes=5),
            GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
            write=lambda chunk: _cancel_on_ready(cancel, bytearray(), chunk),
            cancel=cancel,
        )
    )

    assert outcome.exit_code == 0
    assert outcome.termination_confirmed
    assert trace == ["scancel", *(["squeue"] * 6)]


def _launcher(
    probe: _SlurmProbe, process: _DescendantHeldProcess, threads: SimThreads
) -> AgentGpuLauncher:
    return AgentGpuLauncher(
        AgentGpuConfig(partitions=("gpu",), max_gpus=1, max_time_minutes=5, default_time_minutes=5),
        ("transport", "shell"),
        probe=probe,
        processes=FakePipedProcessLauncher(lambda _start: process),
        threads=threads,
    )


def _cancelled_run(probe: _SlurmProbe, trace: list[str]) -> bytes:
    threads = SimThreads()
    cancel = threads.event()
    process = _DescendantHeldProcess(threads, term_survivors=0, trace=trace)
    launcher = _launcher(probe, process, threads)
    output = bytearray()
    threads.run(
        lambda: launcher.run(
            GpuJobRequest(gpus=1, time_minutes=5),
            GpuCommand(argv=("work",), cwd=Path("workspace"), env={}),
            write=lambda chunk: _cancel_on_ready(cancel, output, chunk),
            cancel=cancel,
        )
    )
    return bytes(output)


_UNKNOWN = st.sampled_from([None, ProbeResult(255, "", "connection lost"), ProbeResult(1, "")])
_LISTED = st.sampled_from([None, ProbeResult(1, ""), ProbeResult(0, "4242\n")])


@settings(max_examples=examples(pr=20, nightly=100))
@given(unknown=st.lists(_UNKNOWN, max_size=4))
def test_scancel_runs_again_while_its_outcome_is_unknown_up_to_a_bound(
    unknown: list[ProbeResult | None],
) -> None:
    """Regression (REVIEW-9 P2-3): one lost ``scancel`` used to count as a cancel."""
    trace: list[str] = []
    _cancelled_run(_SlurmProbe(trace, scancel=[*unknown, ProbeResult(0, "")]), trace)

    assert trace.count("scancel") == min(len(unknown) + 1, 3)


@settings(max_examples=examples(pr=20, nightly=100))
@given(listed=st.lists(_LISTED, max_size=8))
def test_a_cancel_is_reported_unconfirmed_while_squeue_still_lists_or_cannot_answer(
    listed: list[ProbeResult | None],
) -> None:
    """Regression (REVIEW-9 P2-3): a killed wrapper's job kept its GPUs with no report.

    The job counts as gone only after six consecutive successful ``squeue``
    answers list nothing, and the observation window remains bounded.
    """
    trace: list[str] = []
    output = _cancelled_run(_SlurmProbe(trace, squeue=[*listed, ProbeResult(0, "")]), trace)

    confirmed = len(listed) <= 6
    assert trace.count("squeue") == min(len(listed) + 6, 12)
    assert (b"cancel unconfirmed" in output) is not confirmed
