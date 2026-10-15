"""Properties of a stream over arbitrary writes, caps and read windows, through the public API."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_logs.api import LogLimits, MemoryLogStore, StreamOutcome
from vs_logs.api.testing import CONTRACT_OWNER, ContractOwner, no_warning
from vs_logs.read import FromOffset, Tail, reader_of
from vs_sim.api import OutputStream
from vs_sim.api.testing import SimThreads

_WRITES = st.lists(
    st.tuples(st.sampled_from(list(OutputStream)), st.binary(max_size=40)), max_size=20
)


@st.composite
def _limits(draw: st.DrawFn) -> LogLimits:
    max_bytes = draw(st.integers(min_value=1, max_value=120))
    return LogLimits(max_bytes, draw(st.integers(min_value=1, max_value=max_bytes)))


@given(
    writes=_WRITES,
    limits=_limits(),
    windows=st.lists(st.tuples(st.integers(0, 300), st.integers(1, 50)), max_size=10),
)
def test_any_windows_read_exactly_the_retained_bytes_at_their_offsets(
    writes: list[tuple[OutputStream, bytes]],
    limits: LogLimits,
    windows: list[tuple[int, int]],
) -> None:
    store = MemoryLogStore(CONTRACT_OWNER, threads=SimThreads(), warn=no_warning, limits=limits)
    reader = reader_of(store, CONTRACT_OWNER)
    written = b""
    retained_start = 0
    with store.open(ContractOwner(name="p"), "cmd") as stream:
        for pipe, data in writes:
            stream.write(pipe, data)
            written += data
            (info,) = reader.list_streams()
            assert retained_start <= info.retained_start <= info.size == len(written)
            assert info.size - info.retained_start <= limits.max_bytes
            retained_start = info.retained_start
    for offset, max_bytes in windows:
        chunk = reader.read(stream.stream_id, FromOffset(offset), max_bytes)
        assert chunk.offset == max(offset, retained_start)
        assert chunk.data == written[chunk.offset : chunk.offset + max_bytes]
        assert chunk.truncated_before == (offset < retained_start)
        assert chunk.eof == (chunk.next_offset >= len(written))
        assert chunk.closed
        assert sum(span.length for span in chunk.pipes) == len(chunk.data)


@given(writes=_WRITES, chunk_sizes=st.lists(st.integers(1, 30), min_size=1, max_size=5))
def test_any_chunking_of_reads_reassembles_the_stream(
    writes: list[tuple[OutputStream, bytes]], chunk_sizes: list[int]
) -> None:
    store = MemoryLogStore(CONTRACT_OWNER, threads=SimThreads(), warn=no_warning)
    reader = reader_of(store, CONTRACT_OWNER)
    stream = store.open(ContractOwner(name="p"), "cmd")
    for pipe, data in writes:
        stream.write(pipe, data)
    data = b""
    offset = 0
    for index in range(1000):
        chunk = reader.read(
            stream.stream_id, FromOffset(offset), chunk_sizes[index % len(chunk_sizes)]
        )
        data += chunk.data
        offset = chunk.next_offset
        if chunk.eof:
            break
    assert data == b"".join(data for _, data in writes)
    assert not chunk.closed
    stream.close(StreamOutcome.ENDED)
    assert reader.read(stream.stream_id, FromOffset(offset), 1).closed


@given(writes=_WRITES, tail=st.integers(0, 100), max_bytes=st.integers(1, 100))
def test_a_tail_is_the_last_bytes_and_reaches_eof(
    writes: list[tuple[OutputStream, bytes]], tail: int, max_bytes: int
) -> None:
    store = MemoryLogStore(CONTRACT_OWNER, threads=SimThreads(), warn=no_warning)
    reader = reader_of(store, CONTRACT_OWNER)
    with store.open(ContractOwner(name="p"), "cmd") as stream:
        for pipe, data in writes:
            stream.write(pipe, data)
    written = b"".join(data for _, data in writes)
    chunk = reader.read(stream.stream_id, Tail(tail), max_bytes)
    assert chunk.eof
    assert chunk.data == written[len(written) - min(tail, max_bytes, len(written)) :]
