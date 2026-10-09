"""A second host crash while the restarted host is still recovering."""

from __future__ import annotations

from functools import cache
from typing import TYPE_CHECKING

import pytest
from tests.support.crash_harness import (
    after_crash,
    converges_after,
    crash_plan,
    crash_points,
    name,
    run,
)

from vs_faults.api import Boundary, Crossing

if TYPE_CHECKING:
    from collections.abc import Callable

# A second crash while the restarted host is still recovering. The first crash is sampled
# (the first call of each request kind: the effect ran and its observation was lost). The
# second is every crossing of the recovery window: from the restart until the first request
# that is not an inspection, so each recovery write and each inspection is a crash point.
_INSPECTION = "inspect_request"


def _first_of_each_kind() -> tuple[Crossing, ...]:
    seen: set[str] = set()
    first = []
    for crossing in crash_points():
        if crossing.boundary == Boundary.EXECUTOR_REQUEST and crossing.target not in seen:
            seen.add(crossing.target)
            first.append(crossing)
    return tuple(first)


@cache
def _recovery_window(first: Crossing) -> tuple[Crossing, ...]:
    calls = run(crash_plan(first)).gate.calls
    window: list[Crossing] = []
    for crossing in after_crash(calls, first):
        if crossing.boundary == Boundary.EXECUTOR_REQUEST and crossing.target != _INSPECTION:
            break
        window.append(crossing)
    # The write that authorizes the first ordinary request is a dispatch, not recovery.
    return tuple(window[:-1]) if window and window[-1].boundary == Boundary.DURABLE_WRITE else ()


def _sampled(window: tuple[Crossing, ...]) -> tuple[Crossing, ...]:
    """The harness budget is three minutes: every third crossing of a window, and its last."""
    return tuple(c for i, c in enumerate(window) if i % 3 == 0 or i == len(window) - 1)


def _ends(window: tuple[Crossing, ...]) -> tuple[Crossing, ...]:
    """The CI sample: the first, middle and last crossing of a window."""
    return tuple(dict.fromkeys(window[i] for i in (0, len(window) // 2, -1))) if window else ()


def _double_crashes(
    firsts: tuple[Crossing, ...], pick: Callable[[tuple[Crossing, ...]], tuple[Crossing, ...]]
) -> list[object]:
    return [
        pytest.param(first, second, id=f"{name(first)}+{name(second)}")
        for first in firsts
        for second in pick(_recovery_window(first))
    ]


@pytest.mark.parametrize(("first", "second"), _double_crashes(_first_of_each_kind()[::3], _ends))
def test_a_crash_during_recovery_still_converges(first: Crossing, second: Crossing) -> None:
    converges_after(first, second)


@pytest.mark.slow
@pytest.mark.parametrize(("first", "second"), _double_crashes(_first_of_each_kind(), _sampled))
def test_a_crash_during_recovery_of_each_kind_still_converges(
    first: Crossing, second: Crossing
) -> None:
    converges_after(first, second)
