"""Both stores keep what they are given, number events from 1, and survive a torn tail."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.store import (
    FileStore,
    FrameDirection,
    MemoryStore,
    Store,
    decode_events,
    decode_lines,
    encode_events,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def _event(n: int) -> sc.SessionEvent:
    return sc.SessionEvent(delta=sc.SessionDelta(item_id=f"i{n}", text="x" * n))


@pytest.fixture(params=["memory", "file"])
def make(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[], Store]:
    if request.param == "memory":
        store = MemoryStore()
        return lambda: store
    return lambda: FileStore.at(tmp_path / "state")


def test_events_are_numbered_from_one_and_read_after_an_offset(make: Callable[[], Store]) -> None:
    store = make()
    assert store.last_offset() == 0
    numbered = [store.append(_event(n)) for n in range(1, 6)]
    assert [e.offset for e in numbered] == [1, 2, 3, 4, 5]
    assert store.last_offset() == 5
    assert [e.offset for e in store.events_after(0)] == [1, 2, 3, 4, 5]
    assert [e.offset for e in store.events_after(3)] == [4, 5]
    assert store.events_after(5) == []


def test_an_effect_id_is_new_once(make: Callable[[], Store]) -> None:
    store = make()
    assert store.remember_effect("e1") is True
    assert store.remember_effect("e1") is False
    assert store.remember_effect("e2") is True


def test_raw_frames_keep_their_order_and_the_offset_they_followed(
    make: Callable[[], Store],
) -> None:
    store = make()
    store.append_raw(FrameDirection.TO_PROVIDER, '{"a":1}')
    store.append(_event(1))
    store.append_raw(FrameDirection.FROM_PROVIDER, "line with \n newline escaped by json")
    frames = store.raw_frames()
    assert [(f.after_offset, f.direction) for f in frames] == [
        (0, FrameDirection.TO_PROVIDER),
        (1, FrameDirection.FROM_PROVIDER),
    ]


def test_a_reopened_file_store_has_everything(tmp_path: Path) -> None:
    store = FileStore.at(tmp_path)
    for n in range(1, 4):
        store.append(_event(n))
    store.append_raw(FrameDirection.TO_PROVIDER, "frame")
    store.remember_effect("e1")
    again = FileStore.at(tmp_path)
    assert again.events_after(0) == store.events_after(0)
    assert again.raw_frames() == store.raw_frames()
    assert again.remember_effect("e1") is False
    assert again.append(_event(9)).offset == 4


@given(st.lists(st.integers(1, 20), max_size=8), st.integers(0, 30))
def test_a_torn_tail_loses_only_the_record_it_tore(sizes: list[int], cut: int) -> None:
    events = [_event(n) for n in sizes]
    data = encode_events(events)
    torn = data[: max(0, len(data) - cut)]
    kept, used = decode_events(torn)
    assert kept == events[: len(kept)]
    assert used == len(encode_events(kept))
    if cut == 0:
        assert kept == events


@given(st.binary(max_size=200))
def test_decoding_any_bytes_never_raises_and_the_prefix_re_encodes(data: bytes) -> None:
    kept, used = decode_events(data)
    assert used <= len(data)
    assert encode_events(kept) == data[:used]


@given(st.binary(max_size=200))
def test_lines_decode_total(data: bytes) -> None:
    lines, used = decode_lines(data)
    assert used <= len(data)
    assert data[:used].count(b"\n") == len(lines)


def test_reopening_cuts_a_torn_tail_off_the_files(tmp_path: Path) -> None:
    store = FileStore.at(tmp_path)
    store.append(_event(1))
    store.append(_event(2))
    path = tmp_path / "events.log"
    path.write_bytes(path.read_bytes()[:-3])
    again = FileStore.at(tmp_path)
    assert [e.offset for e in again.events_after(0)] == [1]
    assert again.append(_event(3)).offset == 2
    assert [e.offset for e in FileStore.at(tmp_path).events_after(0)] == [1, 2]
