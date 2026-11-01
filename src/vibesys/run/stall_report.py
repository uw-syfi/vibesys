"""What the step loop is waiting on, told to the person who runs it.

The loop never gives up on a tag it cannot clear or whose call has not returned: it waits, and
records why on its ``StallBoard``. A run that waits on a backend or on an error nobody sees is a
run that looks stuck, so this reporter turns every new entry of the board into a warning in the
run's event journal, once per entry and once more when the entry's cause changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.events import CoreEventType, FrameworkSource, FrameworkWarningData

if TYPE_CHECKING:
    from vibesys.run import EventJournal
    from vs_runtime.api.step_runner import Stall, StallBoard
    from vs_sim.api import Sleeper

REPORT_EVERY_SECONDS = 5.0
"""How often the board is read: a stall is reported at most this long after it begins."""


def describe(stall: Stall) -> tuple[str, str]:
    """The warning's summary and detail for ``stall``."""
    summary = f"the run waits on step {stall.tag.root}: {stall.kind.value}"
    detail = (
        "it will be tried again"
        if stall.error is None
        else f"{type(stall.error).__name__}: {stall.error}"
    )
    return summary, detail


class StallReporter:
    """Warns once for each entry of the board, and again when the cause of an entry changes."""

    def __init__(self, board: StallBoard, events: EventJournal) -> None:
        """Report what ``board`` shows into ``events``."""
        self._board = board
        self._events = events
        self._told: dict[str, tuple[str, str]] = {}

    def report(self) -> None:
        """Warn about every entry told differently than before; forget the entries that ended."""
        stalls = self._board.stalls()
        shown = {stall.tag.root: describe(stall) for stall in stalls}
        for tag, (summary, detail) in shown.items():
            if self._told.get(tag) != (summary, detail):
                self._events.emit(
                    CoreEventType.FRAMEWORK_WARNING,
                    summary,
                    data=FrameworkWarningData(
                        summary=summary,
                        detail=detail,
                        source=FrameworkSource.LOOP,
                        source_label="step loop",
                    ),
                )
        self._told = shown

    async def run(self, clock: Sleeper) -> None:
        """Read the board every ``REPORT_EVERY_SECONDS`` until cancelled."""
        while True:
            await clock.sleep(REPORT_EVERY_SECONDS)
            self.report()
