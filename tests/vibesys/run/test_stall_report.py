"""A run that waits on a step says so, once for each cause, and never stays silent."""

from __future__ import annotations

import asyncio
from itertools import pairwise
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.events import CoreEventType, FrameworkWarningData
from vibesys.run import EventJournal
from vibesys.run.stall_report import REPORT_EVERY_SECONDS, StallReporter
from vs_core.api import RequestId
from vs_runtime.api.step_runner import StallBoard, StallKind
from vs_sim.api.testing import SimThreads, VirtualClock, run_virtual

if TYPE_CHECKING:
    from vibesys.events import CoreEvent

pytestmark = pytest.mark.timeout(60)


class ClearRefusedError(RuntimeError):
    """The error a clear raised."""

    def __init__(self, why: str) -> None:
        super().__init__(why)


def warnings(events: EventJournal) -> list[CoreEvent]:
    return [e for e in events.history() if e.type is CoreEventType.FRAMEWORK_WARNING]


def journal() -> tuple[EventJournal, VirtualClock]:
    clock = VirtualClock(at=1.0)
    return EventJournal(clock, SimThreads(clock)), clock


@given(causes=st.lists(st.sampled_from(["a", "b"]), min_size=1, max_size=6))
def test_a_stalled_step_is_reported_once_for_each_cause_it_has(causes: list[str]) -> None:
    events, _clock = journal()
    board = StallBoard()
    reporter = StallReporter(board, events)
    tag = RequestId(root="step-1")

    for why in causes:
        board.note(tag, StallKind.CLEAR_FAILED, ClearRefusedError(why), now=1.0)
        reporter.report()
        reporter.report()

    changes = 1 + sum(1 for before, after in pairwise(causes) if before != after)
    assert len(warnings(events)) == changes


def test_a_step_that_stopped_stalling_is_told_again_if_it_stalls_anew() -> None:
    events, _clock = journal()
    board = StallBoard()
    reporter = StallReporter(board, events)
    tag = RequestId(root="step-1")

    board.note(tag, StallKind.CLEAR_UNAVAILABLE, None, now=1.0)
    reporter.report()
    board.release(tag)
    reporter.report()
    board.note(tag, StallKind.CLEAR_UNAVAILABLE, None, now=9.0)
    reporter.report()

    assert len(warnings(events)) == 2


def test_the_warning_names_the_step_and_what_kept_it_stalled() -> None:
    events, _clock = journal()
    board = StallBoard()
    board.note(
        RequestId(root="dispatch-7"), StallKind.CLEAR_FAILED, ClearRefusedError("tree differs"), 1.0
    )

    StallReporter(board, events).report()

    (warning,) = warnings(events)
    assert isinstance(warning.data, FrameworkWarningData)
    assert "dispatch-7" in warning.data.summary
    assert "clear-failed" in warning.data.summary
    assert warning.data.detail == "ClearRefusedError: tree differs"


def test_the_reporter_reads_the_board_on_the_clock_until_it_is_cancelled() -> None:
    events, clock = journal()
    board = StallBoard()
    reporter = StallReporter(board, events)

    async def main() -> None:
        task = asyncio.ensure_future(reporter.run(clock))
        board.note(RequestId(root="step-1"), StallKind.RUN_HELD, None, clock.now())
        await clock.sleep(REPORT_EVERY_SECONDS * 1.5)
        seen = len(warnings(events))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert seen == 1

    run_virtual(clock, main())
