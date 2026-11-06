"""The lock a process holds for its whole life, which is how the host knows it lives."""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

from vs_sidecar.holder import HolderLock, HolderLockedError

if TYPE_CHECKING:
    from pathlib import Path


def test_a_second_process_cannot_take_a_lock_that_is_held_and_can_once_it_is_freed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "holder.lock"
    first = HolderLock.take(path)
    with pytest.raises(HolderLockedError, match=r"holder\.lock"):
        HolderLock.take(path)
    first.release()
    first.release()  # freeing twice is fine
    with HolderLock.take(path), pytest.raises(HolderLockedError):
        HolderLock.take(path)
    HolderLock.take(path).release()


def test_a_look_at_the_lock_by_the_host_does_not_make_a_starting_process_end(
    tmp_path: Path,
) -> None:
    path = tmp_path / "holder.lock"
    look = HolderLock.take(path)  # the host's try, at the very moment the process tries
    pauses: list[None] = []

    def pause() -> None:
        pauses.append(None)
        look.release()  # the look is over after a moment

    started = HolderLock.take(path, pause=pause)
    assert pauses == [None]
    started.release()


def test_a_process_that_really_holds_the_lock_makes_a_second_one_end_after_its_tries(
    tmp_path: Path,
) -> None:
    path = tmp_path / "holder.lock"
    first = HolderLock.take(path)
    pauses: list[None] = []
    with pytest.raises(HolderLockedError):
        HolderLock.take(path, pause=lambda: pauses.append(None), attempts=5)
    assert len(pauses) == 4  # between its five tries
    first.release()


def test_a_lock_left_by_a_process_that_ended_is_free_whatever_the_file_holds(
    tmp_path: Path,
) -> None:
    path = tmp_path / "holder.lock"
    path.write_text("a pid, or anything else a dead process left")
    HolderLock.take(path).release()


def test_the_lock_is_private_and_is_not_handed_to_the_processes_this_one_starts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "holder.lock"
    with HolderLock.take(path) as lock:
        # An agent that outlives this process must not keep the lock held, or the host would believe
        # this process still lives. The descriptor is the only one the lock has.
        # lint-waiver: LW-770103 [SLF001]; the descriptor is the property under test.
        # > A public accessor would exist only for this check; the lock has no other reader of it.
        descriptor = lock._descriptor  # noqa: SLF001
        assert descriptor is not None
        assert not os.get_inheritable(descriptor)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_a_lock_that_cannot_be_opened_is_not_reported_as_held(tmp_path: Path) -> None:
    with pytest.raises(OSError, match=r"holder\.lock") as raised:
        HolderLock.take(tmp_path / "missing" / "holder.lock")
    assert not isinstance(raised.value, HolderLockedError)
