"""A Slurm stage's output reaches its stream while the job runs, once, and ends with the stage.

The cluster is the Fake: its stage output files are FakeByteSources the test grows between
the executor's scheduler polls (its sleeper), and its timeline runs on a manual clock.
"""

from __future__ import annotations

import asyncio
import math
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    ExecutorObservation,
)
from vs_sandbox.api.slurm import SlurmEvaluationExecutor, SlurmStagePayload
from vs_sim.api import Capture, OutputStream, yield_now
from vs_sim.api.testing import NO_PROCESSES, InlineBlockingRunner, ManualClock, arrival, examples
from vs_slurm.api import (
    FakeCluster,
    SlurmBatchResult,
    SlurmBatchStageResult,
    SlurmConfig,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_POLL_S = 10.0
_HANDLE = "eval-live"
_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED})
_BLOCKING = InlineBlockingRunner()


@dataclass
class _Streams:
    """An opener that keeps each stage's bytes and how its stream ended."""

    output: dict[str, Capture] = field(default_factory=dict)
    ended: dict[str, str] = field(default_factory=dict)
    opened: list[str] = field(default_factory=list)

    @contextmanager
    def open(self, handle_id: str, stage: str) -> Iterator[Capture]:
        assert handle_id == _HANDLE
        self.opened.append(stage)
        sink = self.output[stage] = Capture(1 << 20)
        try:
            yield sink
        except BaseException:
            self.ended[stage] = "failed"
            raise
        self.ended[stage] = "ended"

    def stdout(self, stage: str) -> bytes:
        return self.output[stage].kept(OutputStream.STDOUT) if stage in self.output else b""


class _Polls:
    """The executor's sleeper: runs ``between`` at each poll, then lets the job's time pass."""

    def __init__(self, cluster_clock: ManualClock, between: Callable[[int], None]) -> None:
        self._clock = cluster_clock
        self._between = between
        self.count = 0
        self.polled = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self._between(self.count)
        self.count += 1
        self.polled.set()
        self._clock.advance(seconds)
        await yield_now()


def _result(stages: dict[str, tuple[bytes, int | None]]) -> SlurmBatchResult:
    return SlurmBatchResult(
        job_id="0",
        job_exit_code=0,
        job_output="",
        stages=tuple(
            SlurmBatchStageResult(
                name=name,
                exit_code=exit_code,
                stdout=stdout.decode(),
                stderr="",
                elapsed_seconds=1.0,
                skipped=False,
            )
            for name, (stdout, exit_code) in stages.items()
        ),
        phase_timings_seconds={},
        content_cache_hits=0,
    )


def _request(names: tuple[str, ...]) -> EvaluationRequest:
    return EvaluationRequest(
        key="live",
        stages=tuple(
            EvaluationStep(
                name=name,
                payload=SlurmStagePayload(command="true", timeout_seconds=5).model_dump(
                    mode="json"
                ),
            )
            for name in names
        ),
        stop_on_failure=False,
    )


def _executor(
    root: Path, cluster: FakeCluster, polls: _Polls, streams: _Streams
) -> SlurmEvaluationExecutor:
    workspace = root / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "input.txt").write_text("x", encoding="utf-8")
    return SlurmEvaluationExecutor(
        SlurmConfig(
            name="live",
            remote_workspace_root="/runs",
            transport=SlurmSshTransport(host="live"),
            poll_interval_seconds=_POLL_S,
        ),
        workspace=workspace,
        setup_script=None,
        service=None,
        support_trees={},
        handle_root=root / "handles",
        cluster=cluster,
        sleeper=polls,
        # The job deadline is not under test; a clock that never moves keeps it out.
        clock=ManualClock(),
        stage_output=streams.open,
        blocking=_BLOCKING,
        processes=NO_PROCESSES,
    )


async def _terminal(executor: SlurmEvaluationExecutor) -> ExecutorObservation:
    while True:
        observed = await executor.inspect(_HANDLE)
        if observed is not None and observed.state in _TERMINAL:
            return observed
        await executor.wait_for_change(_HANDLE, timeout_s=math.inf)


async def _growth_arrives_live(root: Path, chunks: list[bytes]) -> None:
    clock = ManualClock()
    cluster = FakeCluster(clock=clock)
    file = cluster.stage_output_file(_HANDLE, 0, OutputStream.STDOUT)
    streams = _Streams()
    live: list[tuple[bytes, bytes]] = []

    def between(poll: int) -> None:
        # What the stream held at this poll, against what the job had written before it.
        written = b"".join(chunks[:poll])
        live.append((streams.stdout("accuracy"), written))
        if poll < len(chunks):
            file.append(chunks[poll])

    polls = _Polls(clock, between)
    whole = b"".join(chunks)
    cluster.script(
        _HANDLE,
        queue_wait_s=0.0,
        # Running through every poll that writes a chunk, and one more that reads the last.
        run_s=_POLL_S * (len(chunks) + 1.5),
        completing_s=0.0,
        result=_result({"accuracy": (whole, 0)}),
    )
    executor = _executor(root, cluster, polls, streams)
    try:
        await executor.submit(_request(("accuracy",)), handle_id=_HANDLE)
        observed = await _terminal(executor)
    finally:
        await executor.close()

    assert observed.state is EvaluationState.SUCCEEDED
    assert live
    assert all(held == written for held, written in live), live
    assert streams.stdout("accuracy") == whole
    assert streams.ended == {"accuracy": "ended"}


@settings(max_examples=examples(pr=4, nightly=30), deadline=None)
@given(chunks=st.lists(st.binary(max_size=40).map(lambda data: data.hex().encode()), max_size=5))
def test_a_running_stage_output_reaches_its_stream_at_each_poll_once_and_in_order(
    tmp_path_factory: pytest.TempPathFactory, chunks: list[bytes]
) -> None:
    """Whatever the job appends between polls is in the stream by the next poll, exactly once."""
    asyncio.run(_growth_arrives_live(tmp_path_factory.mktemp("live"), chunks))


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["truncate", "replace"])
async def test_a_stage_file_that_changes_is_reported_and_never_repeated(
    tmp_path: Path, change: str, caplog: pytest.LogCaptureFixture
) -> None:
    clock = ManualClock()
    cluster = FakeCluster(clock=clock)
    file = cluster.stage_output_file(_HANDLE, 0, OutputStream.STDOUT)
    streams = _Streams()

    def between(poll: int) -> None:
        if poll == 0:
            file.append(b"first line\n")
        elif poll == 1 and change == "truncate":
            # Shorter than what was read: a reader that went on would repeat or skip bytes.
            file.truncate(3)
            file.append(b"x\n")
        elif poll == 1:
            file.replace(b"another file altogether\n")

    cluster.script(
        _HANDLE,
        queue_wait_s=0.0,
        run_s=_POLL_S * 3.5,
        completing_s=0.0,
        result=_result({"accuracy": (b"firx\n", 0)}),
    )
    executor = _executor(tmp_path, cluster, _Polls(clock, between), streams)
    try:
        await executor.submit(_request(("accuracy",)), handle_id=_HANDLE)
        observed = await _terminal(executor)
    finally:
        await executor.close()

    assert observed.state is EvaluationState.SUCCEEDED
    assert streams.stdout("accuracy") == b"first line\n"
    assert streams.ended == {"accuracy": "failed"}
    assert any("not read further" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(("exit_code", "ended"), [(0, "ended"), (3, "ended"), (None, "failed")])
async def test_each_stage_stream_ends_with_its_stage_including_stages_between_polls(
    tmp_path: Path, exit_code: int | None, ended: str
) -> None:
    """A stage that ran to an exit status ends its stream; one without a result fails it.

    Both stages run and end between two polls, so their streams open and fill at settle.
    """
    clock = ManualClock()
    cluster = FakeCluster(clock=clock)
    streams = _Streams()
    cluster.script(
        _HANDLE,
        queue_wait_s=0.0,
        run_s=_POLL_S / 2,
        completing_s=0.0,
        result=_result({"accuracy": (b"checked\n", 0), "benchmark": (b"rps=41\n", exit_code)}),
    )
    executor = _executor(tmp_path, cluster, _Polls(clock, lambda _poll: None), streams)
    try:
        await executor.submit(_request(("accuracy", "benchmark")), handle_id=_HANDLE)
        await _terminal(executor)
    finally:
        await executor.close()

    assert streams.opened == ["accuracy", "benchmark"]
    assert (streams.stdout("accuracy"), streams.stdout("benchmark")) == (b"checked\n", b"rps=41\n")
    assert streams.ended == {"accuracy": "ended", "benchmark": ended}


@pytest.mark.asyncio
async def test_a_cancelled_stage_ends_its_stream_failed(tmp_path: Path) -> None:
    clock = ManualClock()
    cluster = FakeCluster(clock=clock)
    file = cluster.stage_output_file(_HANDLE, 0, OutputStream.STDOUT)
    streams = _Streams()
    polls = _Polls(clock, lambda poll: file.append(f"step {poll}\n".encode()))
    cluster.script(
        _HANDLE,
        queue_wait_s=0.0,
        run_s=math.inf,
        completing_s=0.0,
        result=_result({"accuracy": (b"", None)}),
    )
    executor = _executor(tmp_path, cluster, polls, streams)
    try:
        await executor.submit(_request(("accuracy",)), handle_id=_HANDLE)
        ended = asyncio.ensure_future(_terminal(executor))
        while polls.count < 3:
            polls.polled.clear()
            await arrival(polls.polled.wait(), ended)
        await executor.cancel(_HANDLE)
        observed = await ended
    finally:
        await executor.close()

    assert observed.state is EvaluationState.CANCELED
    assert streams.stdout("accuracy").startswith(b"step 0\nstep 1\n")
    assert streams.ended == {"accuracy": "failed"}
