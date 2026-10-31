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
from vs_core.api.recovery import Cancelled, Finished, interruption_of
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
            accepted=True,
            terminal=True,
            released=True,
            children_complete=True,
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


@dataclass(frozen=True)
class AttemptHolds:
    """The attempt ignores its signal and its task's cancellation for ``for_s`` seconds.

    It is blocking work in a thread: cancelling the task cannot stop it, so the call returns
    only when the work has ended. A cancel that arrived meanwhile is honored then, unless
    ``returns_anyway``, which is a runner that swallows the cancel and returns its result.
    """

    for_s: float
    returns_anyway: bool = False
    result: StepResult | None = None
    """``None`` is ``finished_result`` of the tag."""


type Behavior = AttemptReturns | AttemptRaises | AttemptHangs | AttemptHolds


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


class OverlappingCallError(Exception):
    """A call on a tag began while an earlier call on it was still unfinished."""

    def __init__(self, tag: RequestId, method: str, busy: tuple[str, ...]) -> None:
        """Name the step, the new call and the unfinished ones."""
        super().__init__(f"{method} of {tag} while {', '.join(busy)} of it is still unfinished")


class StillRunningAfterCancelError(Exception):
    """A cancelled attach to adopted work found it alive: the cancel has not taken effect yet."""

    def __init__(self, tag: RequestId) -> None:
        """Name the step."""
        super().__init__(f"{tag} is still running after its cancel")


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
        cancel_needs_polls: int = 0,
    ) -> None:
        """Run on ``clock``; with ``adopts``, ``clear`` adopts work that survives, as Slurm's does.

        Without ``adopts`` every ``clear`` ends all work of the tag, as an agent turn's does.
        Work that is adopted survives the host, so a cancel that only interrupts the attempt (a
        pause) leaves it running for ``clear`` to adopt; a cancel that ends the step ends it.
        ``clear_takes_s`` is how long each ``clear`` takes. ``behave`` chooses what an attempt
        does for a tag with no script; without it the attempt returns ``finished_result``.
        The first ``cancel_needs_polls`` times a cancel ends a call, the work is still alive and
        the call raises ``StillRunningAfterCancelError``, as a job still completing does.
        """
        self._clock = clock
        self._adopts = adopts
        self._clear_takes_s = clear_takes_s
        self._behave = behave
        self._scripts: dict[RequestId, tuple[Behavior, ...]] = {}
        self._work: dict[RequestId, _Work] = {}
        self._attached: set[RequestId] = set()
        self._cancel_ended: list[RequestId] = []
        self._pending: dict[RequestId, list[str]] = {}
        self._resisting: set[RequestId] = set()
        self._threads: set[asyncio.Future[None]] = set()
        self._cancel_polls = cancel_needs_polls
        self._clear_holds: list[float] = []
        self._clear_errors: list[Exception] = []
        self._unavailable = 0
        self._unreachable = False
        self._unreachable_tags: frozenset[RequestId] = frozenset()
        self._calls: list[RunnerCall] = []
        self._violations: list[Exception] = []

    def script(self, tag: RequestId, *behaviors: Behavior) -> None:
        """Make attempt ``n`` of ``tag`` do ``behaviors[n - 1]``; the last repeats."""
        self._scripts[tag] = behaviors

    def clears_unavailable(self, times: int) -> None:
        """Make the next ``times`` calls of ``clear``, of any tag, raise ``ClearUnavailableError``."""
        self._unavailable = times

    def clears_hold(self, for_s: float, times: int = 1) -> None:
        """Make the next ``times`` calls of ``clear`` ignore cancellation for ``for_s`` seconds."""
        self._clear_holds = [for_s] * times

    def clears_fail(self, error: Exception, times: int = 1) -> None:
        """Make the next ``times`` calls of ``clear`` raise ``error``: it has not cleared."""
        self._clear_errors = [error] * times

    @contextmanager
    def unreachable(self, *tags: RequestId) -> Iterator[None]:
        """Make ``clear`` raise ``ClearUnavailableError`` while the context is open.

        For the ``tags`` named, or for every tag when none is: a backend can be reachable for
        one step's work and not for another's.
        """
        before = (self._unreachable, self._unreachable_tags)
        if tags:
            self._unreachable_tags = before[1] | frozenset(tags)
        else:
            self._unreachable = True
        try:
            yield
        finally:
            self._unreachable, self._unreachable_tags = before

    @property
    def calls(self) -> tuple[RunnerCall, ...]:
        """Every call received, in order."""
        return tuple(self._calls)

    @property
    def violations(self) -> tuple[Exception, ...]:
        """The ways the caller broke the contract; a faithful loop leaves none."""
        return tuple(self._violations)

    @property
    def ended_by_cancel(self) -> tuple[RequestId, ...]:
        """The tags whose work a cancel signal ended, in order; a pause that left it is not here."""
        return tuple(self._cancel_ended)

    def running(self) -> tuple[RequestId, ...]:
        """The tags whose work is still going."""
        now = self._clock.now()
        return tuple(tag for tag, work in self._work.items() if work.ended(now) is None)

    def pending(self) -> tuple[RequestId, ...]:
        """The tags with a call of the runner that has not returned."""
        return tuple(tag for tag, calls in self._pending.items() if calls)

    def resisting(self) -> tuple[RequestId, ...]:
        """The tags whose unfinished call was cancelled and carries on: work a cancel cannot stop."""
        return tuple(self._resisting)

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
        self._enter(tag, "run")
        try:
            return await self._run(step, tag, attempt, cancel)
        finally:
            self._leave(tag, "run")

    async def _run(
        self, step: Request, tag: RequestId, attempt: int, cancel: CancelSignal
    ) -> StepResult:
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
            case AttemptHolds(for_s, returns_anyway, result):
                self._work[tag] = _Work(None, None)
                cancelled = await self._held(tag, for_s)
                self._work.pop(tag, None)
                if cancelled and not returns_anyway:
                    raise asyncio.CancelledError
                return result or finished_result(tag)
        ignoring = isinstance(behavior, AttemptHangs) and behavior.ignoring_signal
        return await self._await_end(tag, cancel, ignoring_signal=ignoring)

    async def clear(self, step: Request, tag: RequestId) -> Adopted | Cleared:
        """End the tag's work, or adopt it when this runner adopts."""
        self._calls.append(RunnerCall(RunnerMethod.CLEAR, tag, None, None))
        self._check_identity(step, tag)
        self._enter(tag, "clear")
        try:
            return await self._clear(tag)
        finally:
            self._leave(tag, "clear")

    async def _clear(self, tag: RequestId) -> Adopted | Cleared:
        if self._clear_holds:
            await self._held(tag, self._clear_holds.pop(0))
        if self._clear_takes_s:
            await self._clock.sleep(self._clear_takes_s)
        if self._clear_errors:
            raise self._clear_errors.pop(0)
        if self._unreachable or tag in self._unreachable_tags or self._unavailable:
            self._unavailable = max(0, self._unavailable - 1)
            raise ClearUnavailableError(tag, "the fake backend")
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

    def _enter(self, tag: RequestId, method: str) -> None:
        """Start a call on the tag; one that begins over an unfinished call is a violation."""
        busy = self._pending.setdefault(tag, [])
        if busy:
            if method == "clear" and "run" in busy:
                self._violations.append(ClearedRunningAttemptError(tag))
            else:
                self._violations.append(OverlappingCallError(tag, method, tuple(busy)))
        busy.append(method)

    def _leave(self, tag: RequestId, method: str) -> None:
        calls = self._pending.get(tag, [])
        if method in calls:
            calls.remove(method)
        if not calls:
            self._resisting.discard(tag)

    async def _held(self, tag: RequestId, seconds: float) -> bool:
        """Carry on for ``seconds`` whatever cancels this call, like work in a thread.

        Returns whether the call was cancelled meanwhile.
        """
        work = asyncio.ensure_future(self._clock.sleep(seconds))
        self._threads.add(work)
        cancelled = False
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                if work.cancelled():
                    break
                cancelled = True
                self._resisting.add(tag)
        self._resisting.discard(tag)
        return cancelled

    def process_died(self) -> None:
        """Kill the host process: work in its threads dies with it and no call stays pending."""
        for work in self._threads:
            work.cancel()
        self._threads.clear()
        self._pending.clear()
        self._resisting.clear()

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
        try:
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()
            await asyncio.gather(*waits, return_exceptions=True)
        cancel_won = len(waits) > 1 and waits[1] in done and waits[0] not in done
        if cancel_won and self._cancel_polls > 0:
            self._cancel_polls -= 1
            raise StillRunningAfterCancelError(tag)
        self._attached.discard(tag)
        if cancel_won and self._adopts and interruption_of(cancel.reason) is not None:
            return Cancelled(reason=cancel.reason)
        if cancel_won:
            self._cancel_ended.append(tag)
        self._work.pop(tag, None)
        return next(wait.result() for wait in waits if wait in done)

    async def _ends_by_itself(self, work: _Work) -> StepResult:
        if work.ends_at is None or work.result is None:
            return await asyncio.get_running_loop().create_future()
        await self._clock.sleep(max(0.0, work.ends_at - self._clock.now()))
        return work.result

    @staticmethod
    async def _cancelled(cancel: CancelSignal) -> StepResult:
        return Cancelled(reason=await cancel.wait())
