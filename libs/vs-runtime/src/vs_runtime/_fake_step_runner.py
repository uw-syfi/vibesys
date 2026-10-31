"""An in-memory ``StepRunner`` whose work outlives the task that started it, as a real runner's does.

A real runner's work is a process group or a cluster job: killing the program that started
it, or cancelling the task that awaits it, leaves it running. So does this Fake's. Only
``clear``, or the attempt noticing its cancel signal, ends it, and an attempt started
while an earlier one's work was never cleared is refused, as two agents in one workspace
would be. A test reads the ledger (``calls``, ``running``, ``violations``) after the loop
or the suite has run.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from vs_core.api import EventId, Observation, ObservationStatus, RequestId, RunId, Scope
from vs_core.api.recovery import Cancelled, Finished
from vs_runtime._step_runner import Adopted, CancelSignal, Cleared, ClearUnavailableError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vs_core.api import Request
    from vs_core.api.recovery import StepResult
    from vs_sim.api import SleepingClock


def finished_result(tag: RequestId) -> Finished:
    """The result of a step that ran to the end, the Fake's default."""
    return Finished(
        observation=Observation(
            event_id=EventId(root=f"{tag.root}-done"),
            request_id=tag,
            scope=Scope(owner=RunId(root="run"), generation=0),
            sequence=0,
            observed_at=0.0,
            status=ObservationStatus.SUCCEEDED,
            terminal=True,
        )
    )


@dataclass(frozen=True)
class AttemptReturns:
    """The attempt ends by itself after ``after_s`` seconds with ``result``."""

    result: StepResult | None = None
    """``None`` is ``finished_result`` of the tag."""
    after_s: float = 0.0


@dataclass(frozen=True)
class AttemptRaises:
    """The attempt raises ``error``, leaving its work running: a crash with no result."""

    error: Exception


@dataclass(frozen=True)
class AttemptHangs:
    """The attempt runs until cancelled; the signal ends it, or only the task's cancellation."""

    ignoring_signal: bool = False


type Behavior = AttemptReturns | AttemptRaises | AttemptHangs


class RunnerMethod(StrEnum):
    """Which runner method a ledger entry records."""

    RUN = "run"
    CLEAR = "clear"


@dataclass(frozen=True)
class RunnerCall:
    """One call the runner received, in order."""

    kind: RunnerMethod
    tag: RequestId
    attempt: int | None
    """The attempt of a ``run`` call; ``None`` for ``clear``."""
    deadline_at: float | None
    """The deadline a ``run`` call was given; ``None`` for ``clear``."""


class LeftoverWorkError(Exception):
    """An attempt started while an earlier attempt's work was never cleared."""

    def __init__(self, tag: RequestId, attempt: int) -> None:
        """Name the step and the attempt that was refused."""
        super().__init__(f"attempt {attempt} of {tag} started before clear removed its leftovers")


class StepTagMismatchError(Exception):
    """A call named a tag that is not the request id of the step it passed."""

    def __init__(self, tag: RequestId, step: Request) -> None:
        """Name the tag and the step's own id."""
        super().__init__(f"tag {tag} is not the request id of the step passed with it")
        self.step = step


class ClearedRunningAttemptError(Exception):
    """``clear`` was called while an attempt of the tag was still being awaited."""

    def __init__(self, tag: RequestId) -> None:
        """Name the step."""
        super().__init__(f"clear of {tag} while an attempt of it is still being awaited")


@dataclass
class _Work:
    """What an attempt started in the world: it ends at ``ends_at`` or never by itself."""

    ends_at: float | None
    result: StepResult | None

    def ended(self, now: float) -> StepResult | None:
        return self.result if self.ends_at is not None and self.ends_at <= now else None


class FakeStepRunner:
    """A programmable runner: scripted attempts, work that outlives them, a flaky backend."""

    def __init__(
        self,
        clock: SleepingClock,
        *,
        adopts: bool = False,
        clear_takes_s: float = 0.0,
        behave: Callable[[Request, RequestId, int], Behavior] | None = None,
    ) -> None:
        """Run on ``clock``; with ``adopts``, ``clear`` adopts work that survives, as Slurm's does.

        Without ``adopts`` every ``clear`` ends all work of the tag, as an agent turn's does.
        ``clear_takes_s`` is how long each ``clear`` takes. ``behave`` chooses what an attempt
        does for a tag with no script; without it the attempt returns ``finished_result``.
        """
        self._clock = clock
        self._adopts = adopts
        self._clear_takes_s = clear_takes_s
        self._behave = behave
        self._scripts: dict[RequestId, tuple[Behavior, ...]] = {}
        self._work: dict[RequestId, _Work] = {}
        self._attached: set[RequestId] = set()
        self._awaited: set[RequestId] = set()
        self._unavailable = 0
        self._unreachable = False
        self._calls: list[RunnerCall] = []
        self._violations: list[Exception] = []

    def script(self, tag: RequestId, *behaviors: Behavior) -> None:
        """Make attempt ``n`` of ``tag`` do ``behaviors[n - 1]``; the last repeats."""
        self._scripts[tag] = behaviors

    def clears_unavailable(self, times: int) -> None:
        """Make the next ``times`` calls of ``clear``, of any tag, raise ``ClearUnavailableError``."""
        self._unavailable = times

    @contextmanager
    def unreachable(self) -> Iterator[None]:
        """Make every ``clear`` raise ``ClearUnavailableError`` while the context is open."""
        self._unreachable = True
        try:
            yield
        finally:
            self._unreachable = False

    @property
    def calls(self) -> tuple[RunnerCall, ...]:
        """Every call received, in order."""
        return tuple(self._calls)

    @property
    def violations(self) -> tuple[Exception, ...]:
        """The ways the caller broke the contract; a faithful loop leaves none."""
        return tuple(self._violations)

    def running(self) -> tuple[RequestId, ...]:
        """The tags whose work is still going."""
        now = self._clock.now()
        return tuple(tag for tag, work in self._work.items() if work.ended(now) is None)

    def left_behind(self) -> tuple[RequestId, ...]:
        """The tags with any work in the world, going or ended but never collected."""
        return tuple(self._work)

    async def run(
        self,
        step: Request,
        tag: RequestId,
        attempt: int,
        deadline_at: float,
        cancel: CancelSignal,
    ) -> StepResult:
        """Do what the tag's script says for this attempt, or attach to adopted work."""
        self._calls.append(RunnerCall(RunnerMethod.RUN, tag, attempt, deadline_at))
        self._check_identity(step, tag)
        if tag in self._attached:
            return await self._await_end(tag, cancel, ignoring_signal=False)
        if tag in self._work:
            error = LeftoverWorkError(tag, attempt)
            self._violations.append(error)
            raise error
        behavior = self._behavior(step, tag, attempt)
        now = self._clock.now()
        match behavior:
            case AttemptReturns(result, after_s):
                self._work[tag] = _Work(now + after_s, result or finished_result(tag))
            case AttemptRaises(error):
                self._work[tag] = _Work(None, None)
                raise error
            case AttemptHangs():
                self._work[tag] = _Work(None, None)
        ignoring = isinstance(behavior, AttemptHangs) and behavior.ignoring_signal
        return await self._await_end(tag, cancel, ignoring_signal=ignoring)

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """End the tag's work, or adopt it when this runner adopts."""
        self._calls.append(RunnerCall(RunnerMethod.CLEAR, tag, None, None))
        self._check_identity(step, tag)
        if self._clear_takes_s:
            await self._clock.sleep(self._clear_takes_s)
        if self._unreachable or self._unavailable:
            self._unavailable = max(0, self._unavailable - 1)
            raise ClearUnavailableError(tag, "the fake backend")
        if tag in self._awaited:
            self._violations.append(ClearedRunningAttemptError(tag))
        work = self._work.get(tag)
        if work is None:
            return Cleared()
        if not self._adopts:
            del self._work[tag]
            return Cleared()
        result = work.ended(self._clock.now())
        if result is None:
            self._attached.add(tag)
            return Adopted(None)
        del self._work[tag]
        self._attached.discard(tag)
        return Adopted(result)

    def _check_identity(self, step: Request, tag: RequestId) -> None:
        if step.request_id != tag:
            error = StepTagMismatchError(tag, step)
            self._violations.append(error)
            raise error

    def _behavior(self, step: Request, tag: RequestId, attempt: int) -> Behavior:
        behaviors = self._scripts.get(tag)
        if behaviors:
            return behaviors[min(attempt, len(behaviors)) - 1]
        if self._behave is not None:
            return self._behave(step, tag, attempt)
        return AttemptReturns()

    async def _await_end(
        self, tag: RequestId, cancel: CancelSignal, *, ignoring_signal: bool
    ) -> StepResult:
        """Wait for the tag's work to end by itself or for the signal; a task cancel leaves it."""
        waits = [asyncio.ensure_future(self._ends_by_itself(self._work[tag]))]
        if not ignoring_signal:
            waits.append(asyncio.ensure_future(self._cancelled(cancel)))
        self._awaited.add(tag)
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            self._awaited.discard(tag)
            for wait in waits:
                wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        self._work.pop(tag, None)
        self._attached.discard(tag)
        return next(wait.result() for wait in waits if wait in done)

    async def _ends_by_itself(self, work: _Work) -> StepResult:
        if work.ends_at is None or work.result is None:
            return await asyncio.get_running_loop().create_future()
        await self._clock.sleep(max(0.0, work.ends_at - self._clock.now()))
        return work.result

    @staticmethod
    async def _cancelled(cancel: CancelSignal) -> StepResult:
        return Cancelled(reason=await cancel.wait())
