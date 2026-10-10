"""Release independently owned resources to the end, whatever interrupts the release."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_runtime.contracts import RunCleanupError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable


@dataclass(frozen=True, slots=True)
class _Outcome:
    """How one release ended, and whether anything cancelled it or its caller."""

    cancelled: bool
    failure: BaseException | None = None


async def _finish(task: asyncio.Future[None]) -> _Outcome:
    """Wait for *task* to end however often the caller is cancelled meanwhile."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-948430 [BLE001]; the finished task's outcome is read below, and the remaining releases must still run.
            break
    if task.cancelled():
        return _Outcome(cancelled=True)
    error = task.exception()
    if isinstance(error, asyncio.CancelledError):
        return _Outcome(cancelled=True)
    return _Outcome(cancelled=cancelled, failure=error)


async def release_all(releases: Iterable[Callable[[], Awaitable[None]]], *, failure: str) -> None:
    """Run every release to its end, then report one typed outcome.

    Each release runs in a task of its own, so a cancellation of the caller
    never interrupts it: the cancellation is remembered and the release is
    waited for, as is every later one. A release that itself ends cancelled
    counts as a cancellation, not as a failed release.

    Raises:
        asyncio.CancelledError: the caller was cancelled, or a release ended
            cancelled; every other failure travels as a note on it.
        RunCleanupError: no cancellation, and at least one release failed;
            *failure* is its message and it retains every failure.
    """
    failures: list[BaseException] = []
    cancelled = False
    for release in releases:
        outcome = await _finish(asyncio.ensure_future(release()))
        if outcome.cancelled:
            cancelled = True
        if outcome.failure is not None:
            failures.append(outcome.failure)
    if cancelled:
        cancellation = asyncio.CancelledError()
        for error in failures:
            cancellation.add_note(f"cleanup also failed: {type(error).__name__}: {error}")
        raise cancellation
    if failures:
        raise RunCleanupError(failure, tuple(failures))


__all__ = ["release_all"]
