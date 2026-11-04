"""A file store written by two drain threads through ``LogScope`` and read back by ``vs_logs.read``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st

from vs_logs.api import FileLogStore, LogLimits, LogScope, StreamOutcome
from vs_logs.api.testing import (
    CONTRACT_OWNER,
    ContractOwner,
    bytes_by_pipe,
    no_warning,
    read_everything,
)
from vs_logs.read import Tail, open_log_directory
from vs_sim.api import OutputStream
from vs_sim.api.testing import HANG_GUARD_S, SimThreads

if TYPE_CHECKING:
    import pytest


def _produce(scope: LogScope[ContractOwner], threads: SimThreads, lines: int) -> str:
    """What a process runner does: drain stdout and stderr on two threads into one sink."""
    with scope.open(ContractOwner(name="build"), "docker build .") as sink:

        def drain(pipe: OutputStream) -> None:
            for index in range(lines):
                sink.write(pipe, f"{pipe} {index}\n".encode())

        workers = [
            threads.spawn(lambda pipe=pipe: drain(pipe), name=f"drain-{pipe}", daemon=False)
            for pipe in OutputStream
        ]
        for worker in workers:
            worker.join(HANG_GUARD_S)
        return sink.stream_id


@given(schedule_seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_two_drain_threads_write_one_stream_that_reads_back_per_pipe(
    tmp_path_factory: pytest.TempPathFactory, schedule_seed: int
) -> None:
    directory = tmp_path_factory.mktemp("run-logs")
    threads = SimThreads(schedule_seed=schedule_seed)
    store = FileLogStore(
        directory, CONTRACT_OWNER, threads=threads, warn=no_warning, limits=LogLimits(1 << 20, 16)
    )

    stream_id = threads.run(lambda: _produce(store, threads, lines=12))

    reader = open_log_directory(directory, CONTRACT_OWNER)
    (info,) = reader.list_streams()
    assert (info.owner, info.command, info.outcome) == (
        ContractOwner(name="build"),
        "docker build .",
        StreamOutcome.ENDED,
    )
    chunk = read_everything(reader, stream_id, 10)
    assert chunk.closed
    for pipe, data in bytes_by_pipe(chunk).items():
        assert data == b"".join(f"{pipe} {index}\n".encode() for index in range(12))
    assert reader.read(stream_id, Tail(1), 64).data == b"\n"
