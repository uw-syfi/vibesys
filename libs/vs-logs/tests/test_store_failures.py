"""A store that cannot write never fails the work it logs, and a torn line never breaks it.

A full disk is a :class:`MemoryVolume` with a ``capacity``: an append that does not fit
writes what does and raises, as a short write does. Whatever fills up, writing and
closing never raise, each stream keeps a prefix of its bytes, a stream that lost bytes
is recorded as degraded, and every failure is said through ``warn``. A store reopened
over any prefix of its index, or a reader over any prefix of a stream's pipe marks,
keeps every complete record.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from vs_logs.api import LogLimits, MemoryLogStore, StreamOutcome
from vs_logs.api.testing import (
    CONTRACT_OWNER,
    ContractOwner,
    MemoryVolume,
    no_warning,
    read_everything,
)
from vs_logs.read import reader_of
from vs_sim.api import OutputStream
from vs_sim.api.testing import SimThreads, examples

_INDEX = "index.jsonl"
_WRITES = st.lists(
    st.tuples(st.sampled_from(list(OutputStream)), st.binary(min_size=1, max_size=40)),
    max_size=12,
)


def _store(volume: MemoryVolume, warnings: list[str]) -> MemoryLogStore[ContractOwner]:
    return MemoryLogStore(
        CONTRACT_OWNER,
        threads=SimThreads(),
        warn=warnings.append,
        limits=LogLimits(max_bytes=1 << 20, segment_bytes=16),
        volume=volume,
    )


@settings(max_examples=examples(pr=40, nightly=300))
@given(streams=st.lists(_WRITES, min_size=1, max_size=4), free=st.integers(0, 600))
def test_a_full_disk_never_raises_and_keeps_a_prefix_of_each_stream(
    streams: list[list[tuple[OutputStream, bytes]]], free: int
) -> None:
    volume = MemoryVolume()
    warnings: list[str] = []
    store = _store(volume, warnings)
    volume.capacity = volume.used + free
    opened = []
    for number, writes in enumerate(streams):
        with store.open(ContractOwner(name=str(number)), "cmd") as stream:
            for pipe, data in writes:
                stream.write(pipe, data)
        stream.write(OutputStream.STDOUT, b"after close")
        opened.append((stream.stream_id, b"".join(data for _, data in writes)))

    volume.capacity = None
    listed = {info.stream_id: info for info in reader_of(store, CONTRACT_OWNER).list_streams()}
    for stream_id, written in opened:
        info = listed.get(stream_id)
        if info is None:
            assert warnings, "a stream the index could not record was said"
            continue
        kept = read_everything(reader_of(store, CONTRACT_OWNER), stream_id, 64).data
        assert written.startswith(kept)
        if info.outcome is not None and kept != written:
            assert info.degraded
        if info.degraded:
            assert any(stream_id in warning for warning in warnings)
    assert len(warnings) <= len(streams) + 1


def test_a_stream_degraded_by_a_full_disk_drops_later_bytes_and_says_so_once() -> None:
    volume = MemoryVolume()
    warnings: list[str] = []
    store = _store(volume, warnings)
    with store.open(ContractOwner(name="build"), "docker build") as stream:
        stream.write(OutputStream.STDOUT, b"step 1\n")
        volume.capacity = volume.used + 3
        stream.write(OutputStream.STDOUT, b"step 2\n")
        volume.capacity = None
        stream.write(OutputStream.STDERR, b"step 3\n")

    (info,) = reader_of(store, CONTRACT_OWNER).list_streams()
    data = read_everything(reader_of(store, CONTRACT_OWNER), info.stream_id, 64).data
    assert (data, info.outcome, info.degraded) == (b"step 1\nste", StreamOutcome.ENDED, True)
    assert len(warnings) == 1
    assert info.stream_id in warnings[0]


def test_a_stream_the_index_cannot_record_takes_writes_and_closes_quietly() -> None:
    volume = MemoryVolume()
    warnings: list[str] = []
    store = _store(volume, warnings)
    volume.capacity = volume.used + 5
    with store.open(ContractOwner(name="lost"), "cmd") as lost:
        lost.write(OutputStream.STDOUT, b"nowhere")
    volume.capacity = None
    with store.open(ContractOwner(name="kept"), "cmd") as kept:
        kept.write(OutputStream.STDOUT, b"here")

    (info,) = reader_of(store, CONTRACT_OWNER).list_streams()
    assert (info.owner.name, info.outcome, info.degraded) == ("kept", StreamOutcome.ENDED, False)
    assert lost.stream_id != kept.stream_id
    assert len(warnings) == 1


def _written_store(streams: list[list[tuple[OutputStream, bytes]]]) -> MemoryVolume:
    volume = MemoryVolume()
    store = MemoryLogStore(
        CONTRACT_OWNER,
        threads=SimThreads(),
        warn=no_warning,
        limits=LogLimits(max_bytes=1 << 20, segment_bytes=16),
        volume=volume,
    )
    for number, writes in enumerate(streams):
        stream = store.open(ContractOwner(name=str(number)), "cmd")
        for pipe, data in writes:
            stream.write(pipe, data)
        if number % 2 == 0:
            stream.close(StreamOutcome.ENDED)
    return volume


def _index_lines(volume: MemoryVolume) -> list[bytes]:
    data = volume.source(_INDEX).read(0, 1 << 20)
    return data.splitlines(keepends=True)


@settings(max_examples=examples(pr=40, nightly=300))
@given(streams=st.lists(_WRITES, min_size=1, max_size=4), draw=st.data())
def test_a_store_reopened_over_any_prefix_of_its_index_keeps_every_complete_record(
    streams: list[list[tuple[OutputStream, bytes]]], draw: st.DataObject
) -> None:
    volume = _written_store(streams)
    whole = b"".join(_index_lines(volume))
    cut = draw.draw(st.integers(0, len(whole)), label="cut")
    volume.replace(_INDEX, whole[:cut])
    complete = whole[: whole.rfind(b"\n", 0, cut) + 1]
    before = MemoryLogStore(
        CONTRACT_OWNER, threads=SimThreads(), warn=no_warning, volume=MemoryVolume()
    )
    before.memory_volume.replace(_INDEX, complete)
    expected = [info.owner for info in reader_of(before, CONTRACT_OWNER).list_streams()]

    reopened = MemoryLogStore(CONTRACT_OWNER, threads=SimThreads(), warn=no_warning, volume=volume)
    reopened.open(ContractOwner(name="next"), "cmd").close(StreamOutcome.ENDED)

    listed = reader_of(reopened, CONTRACT_OWNER).list_streams()
    assert [info.owner for info in listed] == [*expected, ContractOwner(name="next")]
    assert all(info.outcome is not None for info in listed)
    assert len({info.stream_id for info in listed}) == len(listed)


@settings(max_examples=examples(pr=40, nightly=300))
@given(writes=_WRITES, draw=st.data())
def test_a_reader_over_any_prefix_of_a_streams_pipe_marks_reads_every_byte(
    writes: list[tuple[OutputStream, bytes]], draw: st.DataObject
) -> None:
    volume = _written_store([writes])
    marks = "000001/pipes.jsonl"
    whole = volume.source(marks).read(0, 1 << 20) if writes else b""
    cut = draw.draw(st.integers(0, len(whole)), label="cut")
    volume.replace(marks, whole[:cut])

    store = MemoryLogStore(CONTRACT_OWNER, threads=SimThreads(), warn=no_warning, volume=volume)
    (info,) = reader_of(store, CONTRACT_OWNER).list_streams()
    chunk = read_everything(reader_of(store, CONTRACT_OWNER), info.stream_id, 7)
    assert chunk.data == b"".join(data for _, data in writes)
    assert sum(span.length for span in chunk.pipes) == len(chunk.data)
