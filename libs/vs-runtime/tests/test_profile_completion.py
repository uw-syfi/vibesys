"""Profile completion orders unlocked measurement before locked durable settlement."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api import CandidateProfile, CandidateProfileStatus, complete_profile

if TYPE_CHECKING:
    from collections.abc import Awaitable


class _CaptureFailureError(RuntimeError):
    pass


class _CommitFailureError(RuntimeError):
    pass


@pytest.mark.asyncio
async def test_capture_does_not_hold_the_lock_and_commit_keeps_it() -> None:
    lock = asyncio.Lock()
    capture_started = asyncio.Event()
    release_capture = asyncio.Event()
    commit_started = asyncio.Event()
    release_commit = asyncio.Event()
    observed = CandidateProfile(revision="r", status=CandidateProfileStatus.OBSERVED)
    recorded: list[CandidateProfile] = []

    async def capture() -> CandidateProfile:
        assert not lock.locked()
        capture_started.set()
        await release_capture.wait()
        return observed

    async def commit() -> None:
        assert lock.locked()
        commit_started.set()
        await release_commit.wait()
        assert lock.locked()

    def record(outcome: CandidateProfile) -> Awaitable[None]:
        assert lock.locked()
        recorded.append(outcome)
        return commit()

    task = asyncio.create_task(complete_profile(capture, lock, record))
    await capture_started.wait()
    async with lock:
        assert not recorded
    release_capture.set()
    await commit_started.wait()
    assert recorded == [observed]
    assert lock.locked()
    release_commit.set()
    await task
    assert not lock.locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["capture", "record", "commit"])
async def test_completion_failure_propagates_and_releases_the_lock(failure: str) -> None:
    lock = asyncio.Lock()
    recorded: list[CandidateProfile] = []
    observed = CandidateProfile(revision="r", status=CandidateProfileStatus.OBSERVED)

    async def capture() -> CandidateProfile:
        if failure == "capture":
            raise _CaptureFailureError
        return observed

    async def commit() -> None:
        raise _CommitFailureError

    def record(outcome: CandidateProfile) -> Awaitable[None]:
        if failure == "record":
            raise _CommitFailureError
        recorded.append(outcome)
        return commit()

    expected = _CaptureFailureError if failure == "capture" else _CommitFailureError
    with pytest.raises(expected):
        await complete_profile(capture, lock, record)
    assert recorded == ([observed] if failure == "commit" else [])
    assert not lock.locked()


@pytest.mark.asyncio
async def test_completed_intent_neither_captures_nor_records() -> None:
    lock = asyncio.Lock()

    def record(_outcome: CandidateProfile) -> Awaitable[None]:
        raise _CommitFailureError

    await complete_profile(None, lock, record)
    assert not lock.locked()
