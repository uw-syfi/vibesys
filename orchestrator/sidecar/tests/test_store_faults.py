"""A write that fails, tears or crashes never leaves the log showing what the disk does not hold.

``FaultyFiles`` fails or crashes the Nth append after writing a prefix of it. The invariants, for
every position and every prefix length: what ``events_after`` returned is what a reopened store
holds (nothing renumbered), the reopened offsets are 1..n, and an offset is never given to two
different events.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tests.fakes import CrashError, FaultyFiles, FileFaultError
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc
from vs_sidecar.store import FileStore, StoreError


def _event(n: int) -> sc.SessionEvent:
    return sc.SessionEvent(delta=sc.SessionDelta(item_id=f"i{n}", text="x" * n))


def _offsets(events: list[sc.SessionEvent]) -> list[int]:
    return [e.offset for e in events]


@given(
    count=st.integers(1, 6),
    at=st.integers(1, 6),
    keep=st.integers(0, 40),
)
def test_a_failed_write_is_cut_off_and_the_log_goes_on_from_the_last_event_shown(
    count: int, at: int, keep: int
) -> None:
    files = FaultyFiles(fail_at=at, keep=keep)
    store = FileStore(files)
    shown: list[sc.SessionEvent] = []
    failed = False
    for n in range(1, count + 1):
        try:
            shown.append(store.append(_event(n)))
        except FileFaultError:
            failed = True
    assert failed == (at <= count)
    assert store.events_after(0) == shown
    again = FileStore(files.reopened())
    assert again.events_after(0) == shown
    assert _offsets(again.events_after(0)) == list(range(1, len(shown) + 1))
    assert again.append(_event(9)).offset == len(shown) + 1


@given(count=st.integers(1, 6), at=st.integers(1, 6), keep=st.integers(0, 40))
def test_a_crash_at_any_append_leaves_a_log_that_extends_what_was_shown_by_at_most_the_one_in_flight(
    count: int, at: int, keep: int
) -> None:
    files = FaultyFiles(crash_at=at, keep=keep)
    store = FileStore(files)
    for n in range(1, count + 1):
        try:
            store.append(_event(n))
        except CrashError:
            break
    shown = store.events_after(0)
    again = FileStore(files.reopened())
    kept = again.events_after(0)
    assert kept[: len(shown)] == shown, "nothing a reader saw is lost or renumbered"
    assert len(kept) - len(shown) <= 1
    assert _offsets(kept) == list(range(1, len(kept) + 1))
    # The next event takes the next offset, and the ones before it are unchanged.
    assert again.append(_event(9)).offset == len(kept) + 1
    assert FileStore(files.reopened()).events_after(0)[: len(kept)] == kept


def test_a_write_that_fails_and_cannot_be_undone_stops_the_store_and_a_restart_repairs_it() -> None:
    files = FaultyFiles(fail_at=2, keep=3, fail_truncate=True)
    store = FileStore(files)
    first = store.append(_event(1))
    with pytest.raises(StoreError):
        store.append(_event(2))
    with pytest.raises(StoreError):
        store.append(_event(3))
    assert store.events_after(0) == [first]
    again = FileStore(files.reopened())
    assert again.events_after(0) == [first]
    assert again.append(_event(4)).offset == 2


def test_an_effect_id_whose_write_failed_is_not_remembered() -> None:
    files = FaultyFiles(fail_at=1, keep=0)
    store = FileStore(files)
    with pytest.raises(FileFaultError):
        store.remember_effect("e1")
    assert store.remember_effect("e1") is True
