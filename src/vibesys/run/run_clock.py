"""The run's clock: seconds the run has run, continuing from its log across processes.

The log's records carry a run clock that excludes the time between processes, and core's
deadlines and waits are on it. A process opens its run late, so it reads the clock of the last
record it finds and counts on from there with the wall clock's rate: a pause of an hour or a
crash costs the run no time, and a deadline of one hour still gives one hour of running.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from vs_proto.api import Timestamp

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from vs_proto.api import Record
    from vs_sim.api import SleepingClock


class RunClockNotStartedError(RuntimeError):
    """The run clock was read before the run's log was open."""

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("the run clock is read before the run's log was opened")


class RunClockStartedTwiceError(RuntimeError):
    """The run clock was started again: one process opens one log once."""

    def __init__(self) -> None:
        """Say what happened."""
        super().__init__("the run clock was started twice")


class RunClock:
    """Run seconds over a wall clock; ``start_at`` fixes where this process's count begins.

    The run does not age while the loop runs one synchronous stretch: every reading before the
    event loop next gets control is the same instant. A fact the loop dates and the record that
    holds it are made in one such stretch, so the record never dates itself before the fact.
    The run's log dates every record by this clock (``current``), the display projection's
    included, so no record of the stretch is dated after the instant the fact was.
    """

    def __init__(self, wall: SleepingClock) -> None:
        """Count run seconds at the rate of ``wall``; not yet started."""
        self._wall = wall
        self._shift: float | None = None
        self._instant: float | None = None

    def start_at(self, run_seconds: float, *, wall_at: float | None = None) -> None:
        """Begin the count at ``run_seconds`` as of the wall time ``wall_at`` (now by default).

        The log's writer dates its own records from its epoch header, which carries the run
        seconds of the epoch's start and the wall time it was written at. Counting from the same
        pair puts the records of the loop and the records of the writer on one timeline.
        """
        if self._shift is not None:
            raise RunClockStartedTwiceError
        self._shift = (self._wall.now() if wall_at is None else wall_at) - run_seconds

    def now(self) -> float:
        """Seconds the run has run, as of the stretch of the event loop that reads it."""
        if self._shift is None:
            raise RunClockNotStartedError
        if self._instant is not None:
            return self._instant
        instant = self._wall.now() - self._shift
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return instant
        self._instant = instant
        loop.call_soon(self._pass)
        return instant

    def _pass(self) -> None:
        self._instant = None

    def current(self) -> float | None:
        """``now``, or None while the clock has not started (the run's log is not yet open)."""
        return None if self._shift is None else self.now()

    async def sleep(self, seconds: float) -> None:
        """Wait on the wall clock."""
        await self._wall.sleep(seconds)

    def stamp(self) -> Timestamp:
        """The wall time now, which dates a record beside the run time it carries."""
        return Timestamp.from_datetime(datetime.fromtimestamp(self._wall.now(), UTC))


def start_clock_from(clock: RunClock, records: Sequence[Record], epoch: int) -> None:
    """Start ``clock`` where the writer of ``epoch`` began its count, else at the log's last record."""
    header = next(
        (
            record
            for record in reversed(records)
            if record.epoch == epoch
            and record.body is not None
            and record.body.field == "epoch_started"
            and record.at is not None
            and record.has_field("clock")
        ),
        None,
    )
    if header is None or header.at is None:
        clock.start_at(last_run_clock(records))
    else:
        clock.start_at(header.clock, wall_at=header.at.to_datetime().timestamp())


def last_run_clock(records: Iterable[Record]) -> float:
    """The run clock of the newest record that carries one; 0 for a log that holds none."""
    return max((record.clock for record in records if record.has_field("clock")), default=0.0)
