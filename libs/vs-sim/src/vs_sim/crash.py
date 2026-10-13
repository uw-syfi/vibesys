"""Building blocks for tests that kill a simulated host at chosen points and restart it.

A crash test records the points a fault-free run crosses, then re-runs it once per chosen
point with a host death planted there. The domain decides what a point is and what dying
there means; these helpers own the restart loop and the choice of which points to try.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Hashable, Iterator, Sequence


class RestartLimitError(AssertionError):
    """The host kept crashing past the number of deaths the test planned for."""


@dataclass(frozen=True)
class Restarted[T]:
    """A run that ended after ``crashes`` host deaths."""

    result: T
    crashes: int


async def restart_until_done[T](
    boot: Callable[[int], Awaitable[T]],
    *,
    crashed: type[BaseException] | tuple[type[BaseException], ...],
    max_crashes: int,
) -> Restarted[T]:
    """Start a host with ``boot(generation)`` until one of them ends without dying.

    ``boot`` receives how many hosts have died so far (0 for the first). An exception of
    type ``crashed`` is a host death: the next generation starts over the same durable
    state. Any other outcome ends the run.

    Raises:
        RestartLimitError: more than ``max_crashes`` hosts died.
    """
    for generation in range(max_crashes + 1):
        try:
            return Restarted(await boot(generation), generation)
        except crashed:
            continue
    message = f"the host kept crashing past the {max_crashes} deaths the test planned"
    raise RestartLimitError(message)


def first_of_each_kind[K: Hashable](kinds: Sequence[K]) -> dict[K, int]:
    """The index of the first occurrence of each kind, in order of first appearance."""
    firsts: dict[K, int] = {}
    for index, kind in enumerate(kinds):
        firsts.setdefault(kind, index)
    return firsts


def evenly_spaced(count: int, samples: int) -> list[int]:
    """Up to ``samples`` distinct indexes in ``range(count)``, always including the first and last."""
    if count <= 0 or samples <= 0:
        return []
    if samples >= count:
        return list(range(count))
    if samples == 1:
        return [0]
    return sorted({round(i * (count - 1) / (samples - 1)) for i in range(samples)})


def after_crash[T](recorded: Sequence[T], crash_index: int) -> Sequence[T]:
    """What the restarted host recorded: everything after the point the first host died at."""
    return recorded[crash_index + 1 :]


class SweepBreadth(StrEnum):
    """How many of its crash points a sweep tries: the one setting of a test session.

    A pull request samples every sweep; the nightly job tries every point. The pytest plugin
    sets it from ``--crash-points``, and no test picks its own.
    """

    SAMPLED = "sampled"
    ALL = "all"


class _Breadth:
    """The one place the session's breadth lives."""

    value = SweepBreadth.SAMPLED


def sweep_breadth() -> SweepBreadth:
    """The breadth every crash sweep of this session follows."""
    return _Breadth.value


def set_sweep_breadth(breadth: SweepBreadth) -> None:
    """Choose the breadth for the rest of the session (the pytest plugin does, from its option)."""
    _Breadth.value = breadth


@contextmanager
def using_sweep_breadth(breadth: SweepBreadth) -> Iterator[None]:
    """Follow ``breadth`` inside the block, then return to the previous one."""
    previous = sweep_breadth()
    set_sweep_breadth(breadth)
    try:
        yield
    finally:
        set_sweep_breadth(previous)


def swept(count: int, sample: int) -> list[int]:
    """The indexes of ``range(count)`` a crash sweep tries under the session's breadth.

    Every one under ``SweepBreadth.ALL``; otherwise ``sample`` evenly spaced ones, always
    including the first and the last (`evenly_spaced`).
    """
    if sweep_breadth() is SweepBreadth.ALL:
        return list(range(count))
    return evenly_spaced(count, sample)
