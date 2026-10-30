"""An in-memory ``StepLog``: durable entries, a fold into state, and inputs from elsewhere."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sim.api.testing import Changes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from vs_core.api import RequestId
    from vs_core.api.recovery import End, Run, StepResult
    from vs_runtime._step_loop import Interruption


@dataclass(frozen=True)
class LoggedStarted:
    """An attempt was recorded as about to start."""

    run: Run


@dataclass(frozen=True)
class LoggedFinished:
    """A step was recorded as ended with a result."""

    tag: RequestId
    result: StepResult


@dataclass(frozen=True)
class LoggedInterrupted:
    """An attempt was recorded as ended without ending its step."""

    tag: RequestId
    attempt: int
    why: Interruption


@dataclass(frozen=True)
class LoggedEnded:
    """The run was recorded as ended."""

    end: End


type LogEntry = LoggedStarted | LoggedFinished | LoggedInterrupted | LoggedEnded


class FakeStepLog[S, I]:
    """Entries the loop appends and inputs a test pushes, folded in order into one state.

    ``entries`` is what survived: a restarted loop gets a new log built from it, and nothing
    else carries over, as nothing but the file does.
    """

    def __init__(
        self,
        seed: S,
        fold: Callable[[S, LogEntry | I], S],
        entries: Iterable[LogEntry | I] = (),
    ) -> None:
        """Fold ``entries`` over ``seed``; ``fold`` applies one entry to a state."""
        self._fold = fold
        self._state = seed
        self.entries: list[LogEntry | I] = []
        self._inputs = Changes()
        self._inputs_seen = 0
        self._latest: dict[RequestId, Run] = {}
        for entry in entries:
            self._add(entry)

    @property
    def state(self) -> S:
        """The fold of every entry so far."""
        return self._state

    def push(self, entry: I) -> None:
        """Append an input that arrived from outside the loop, and wake it."""
        self._add(entry)
        self._inputs.notify()

    def latest_run(self, tag: RequestId) -> Run | None:
        """The run of the last ``LoggedStarted`` of ``tag``."""
        return self._latest.get(tag)

    async def step_started(self, run: Run) -> None:
        """Append ``LoggedStarted``."""
        self._add(LoggedStarted(run))

    async def step_finished(self, tag: RequestId, result: StepResult) -> None:
        """Append ``LoggedFinished``."""
        self._add(LoggedFinished(tag, result))

    async def attempt_interrupted(self, tag: RequestId, attempt: int, why: Interruption) -> None:
        """Append ``LoggedInterrupted``."""
        self._add(LoggedInterrupted(tag, attempt, why))

    async def run_ended(self, end: End) -> None:
        """Append ``LoggedEnded``."""
        self._add(LoggedEnded(end))

    async def next_input(self) -> None:
        """Return once an input was pushed since this last returned."""
        await self._inputs.changed(self._inputs_seen)
        self._inputs_seen = self._inputs.version

    def _add(self, entry: LogEntry | I) -> None:
        self.entries.append(entry)
        if isinstance(entry, LoggedStarted):
            self._latest[entry.run.tag] = entry.run
        self._state = self._fold(self._state, entry)
