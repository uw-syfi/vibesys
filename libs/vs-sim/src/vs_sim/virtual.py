"""One virtual clock: waiting costs no wall time and every waiter shares one timeline.

``VirtualClock`` is backed by an event loop whose time *is* the clock: ``run_virtual``
runs a coroutine on a loop that never blocks on a timer. When every task is waiting,
the loop jumps the clock to the earliest timer and runs what is due there. Because a
jump happens only when no task is runnable, sleeps overlap the way real ones do, and
any number of tasks can wait at once without deadlocking each other.

Everything on that loop must wait through the loop or the clock (``asyncio.sleep``,
``clock.sleep``). Work handed to a worker thread (``asyncio.to_thread``,
``run_in_executor``) is tracked: while any such call is in flight the clock does not
jump, because the thread's result may change what the loop does at the current time, so
time passes only once the workers are done. A loop that is idle with nothing scheduled
and no worker in flight cannot ever wake, so it raises instead of hanging; a worker that
does not finish within ``WORKER_GUARD_S`` is reported the same way.

By default ties are broken in the order the work was started, which is what makes a run
repeatable but also hides tests that only pass in that one order. ``run_virtual`` takes a
``schedule_seed`` that breaks every tie differently instead: callbacks that are ready at the
same time run in a random order, and timers due at the same instant fire in a random order.
The order is a pure function of the seed (so a failure replays), no callback is dropped or
delayed past its due time, and the clock still moves only when the loop is idle.
"""

from __future__ import annotations

import asyncio
import collections.abc
import math
import selectors
from typing import TYPE_CHECKING, Any, overload

from vs_sim.randomness import SeededRandom

if TYPE_CHECKING:
    import concurrent.futures
    import contextvars
    from collections.abc import Callable, Coroutine, Generator
    from types import TracebackType

    from vs_sim.trace import EventTrace


WORKER_GUARD_S = 60.0
"""Real seconds a worker thread may run before the loop gives up on it; a hang guard only."""


class VirtualDeadlockError(RuntimeError):
    """Every task is waiting and no timer is scheduled: nothing can ever wake the run."""


class VirtualTimeLimitError(RuntimeError):
    """A run slept past ``VirtualClock.limit``: it is stuck, not slow, because virtual time is free."""


_TIE_BREAK_STEPS = 1 << 16
"""Float steps a seeded schedule may add to a timer's due time; far below any real delay."""

_DUE_MEMORY = 4096
"""Due instants remembered before the ones already past are forgotten."""


class _VirtualSelector(selectors.DefaultSelector):
    """The loop's selector, with timer waits turned into clock jumps."""

    def __init__(self, clock: VirtualClock, trace: EventTrace | None) -> None:
        super().__init__()
        self._clock = clock
        self._trace = trace
        self.loop: _VirtualLoop | None = None

    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        ready = super().select(0)
        if ready or timeout == 0:
            if ready and self._trace is not None:
                self._trace.io(len(ready))
            return ready
        if self.loop is not None and self.loop.workers_in_flight > 0:
            # Time stands still while a worker thread runs; its completion wakes this select.
            ready = super().select(WORKER_GUARD_S)
            if not ready:
                message = (
                    f"a worker thread ran for {WORKER_GUARD_S:g} s without finishing: "
                    f"{self._describe_waiters()}"
                )
                raise VirtualDeadlockError(message)
            if self._trace is not None:
                self._trace.io(len(ready))
            return ready
        if timeout is None:
            raise VirtualDeadlockError(self._describe_waiters())
        if self._trace is not None:
            self._trace.advance(self._clock.at, self._clock.at + timeout)
        self._clock.at += timeout
        return []

    def _describe_waiters(self) -> str:
        message = "every task is waiting and no timer is scheduled"
        if self.loop is None:
            return message
        waiting = sorted(repr(task) for task in asyncio.all_tasks(self.loop))
        hint = (
            "; a task waiting on a real thread, process or socket cannot be woken here "
            "(run blocking work through a BlockingRunner)"
        )
        return f"{message}{hint}: {waiting}"


class _RecordedCoroutine(collections.abc.Coroutine[object, object, object]):
    """A coroutine that reports each scheduling step of its task to the trace."""

    def __init__(
        self, inner: Coroutine[object, object, object], label: str, trace: EventTrace
    ) -> None:
        self._inner = inner
        self._label = label
        self._trace = trace

    def send(self, value: object) -> object:
        self._trace.step(self._label)
        return self._inner.send(value)

    @overload
    def throw(
        self, typ: type[BaseException], val: object = None, tb: TracebackType | None = None, /
    ) -> object: ...

    @overload
    def throw(
        self, typ: BaseException, val: None = None, tb: TracebackType | None = None, /
    ) -> object: ...

    def throw(
        self,
        typ: type[BaseException] | BaseException,
        val: object = None,
        tb: TracebackType | None = None,
        /,
    ) -> object:
        self._trace.step(self._label)
        # The overloads allow only the two spellings Coroutine.throw accepts; the checker
        # cannot see through the union to pick one.
        return self._inner.throw(typ, val, tb)  # ty: ignore[no-matching-overload]

    def close(self) -> None:
        self._inner.close()

    def __await__(self) -> Generator[object, object, object]:
        return self._inner.__await__()


class _VirtualLoop(asyncio.SelectorEventLoop):
    def __init__(
        self, clock: VirtualClock, trace: EventTrace | None, schedule_seed: int | None
    ) -> None:
        selector = _VirtualSelector(clock, trace)
        super().__init__(selector)
        selector.loop = self
        self._clock = clock
        self._schedule = None if schedule_seed is None else SeededRandom(schedule_seed)
        self._taken: set[float] = set()
        self.clock = clock
        self._due: dict[float, float] = {}
        self.workers_in_flight = 0
        if trace is not None:
            self._created = 0
            self.set_task_factory(self._recording_task)
            self._trace = trace

    def time(self) -> float:
        return self._clock.at

    def call_at(
        self,
        when: float,
        callback: Callable[..., object],
        *args: object,
        context: contextvars.Context | None = None,
    ) -> asyncio.TimerHandle:
        # asyncio keeps timers in a heap that is not stable: timers due at the same instant
        # pop in an arbitrary order, and on this loop every sleep started in one turn has
        # the same due time. Nudge each later timer for an instant by one float step so
        # equal sleeps wake in the order they started. The clock itself still jumps to
        # the first due time, and the nudge is far inside the loop's timer resolution.
        if self._schedule is not None:
            due = self._seeded_due(self._schedule, when)
            return super().call_at(due, callback, *args, context=context)
        if len(self._due) > _DUE_MEMORY:
            self._due = {at: last for at, last in self._due.items() if at > self._clock.at}
        last = self._due.get(when)
        due = when if last is None else math.nextafter(last, math.inf)
        self._due[when] = due
        return super().call_at(due, callback, *args, context=context)

    def _seeded_due(self, schedule: SeededRandom, when: float) -> float:
        # Under a schedule seed every timer gets its own due time a random few float steps
        # after the requested one, so timers meant for one instant fire in a seeded order.
        if not math.isfinite(when):
            return when
        if len(self._taken) > _DUE_MEMORY:
            self._taken = {at for at in self._taken if at > self._clock.at}
        step = math.ulp(when)
        while True:
            due = when + step * schedule.randint(0, _TIE_BREAK_STEPS - 1)
            if due not in self._taken:
                self._taken.add(due)
                return due

    def call_soon(
        self,
        callback: Callable[..., object],
        *args: object,
        context: contextvars.Context | None = None,
    ) -> asyncio.Handle:
        handle = super().call_soon(callback, *args, context=context)
        if self._schedule is not None:
            # Ready callbacks run in queue order; put the new one at a seeded position
            # instead of the back. `_ready` is asyncio's own queue, which this loop subclasses.
            # The standard library's queue is not in the type stubs.
            ready = self._ready  # ty: ignore[unresolved-attribute]
            ready.pop()
            ready.insert(self._schedule.randint(0, len(ready)), handle)
        return handle

    def run_in_executor[*Ts, T](
        self,
        executor: concurrent.futures.Executor | None,
        func: Callable[[*Ts], T],
        *args: *Ts,
    ) -> asyncio.Future[T]:
        # Count the call until its future is done, so the selector can tell "idle" from
        # "waiting for a thread". A call whose waiter was cancelled stops counting at once.
        self.workers_in_flight += 1
        future = super().run_in_executor(executor, func, *args)
        future.add_done_callback(self._worker_done)
        return future

    async def shutdown_default_executor(self, timeout: float | None = None) -> None:  # noqa: ASYNC109  # LW-163806 [ASYNC109]; the parameter is the base class signature we override, and it is forwarded unchanged.
        # The default executor's shutdown joins its threads from a helper thread; count it
        # as a worker so the closing loop waits for it instead of reporting a deadlock.
        self.workers_in_flight += 1
        try:
            await super().shutdown_default_executor(timeout)
        finally:
            self.workers_in_flight -= 1

    def _worker_done(self, _future: asyncio.Future[Any]) -> None:
        self.workers_in_flight -= 1

    def _recording_task(
        self,
        loop: asyncio.AbstractEventLoop,
        coro: Coroutine[object, object, object],
        *,
        context: contextvars.Context | None = None,
    ) -> asyncio.Task[object]:
        # Task names count process-wide, so label by creation order within this loop.
        label = f"{getattr(coro, '__qualname__', type(coro).__name__)}#{self._created}"
        self._created += 1
        return asyncio.Task(
            _RecordedCoroutine(coro, label, self._trace), loop=loop, context=context
        )


class VirtualClock:
    """Seconds on the shared virtual timeline. ``sleep`` waits for the timeline, not the wall."""

    def __init__(self, at: float = 1.0, *, limit: float | None = None) -> None:
        """Start the timeline at ``at`` seconds; sleeping past ``limit`` (if set) is an error."""
        self.at = at
        self.limit = limit
        self.sleeps: list[float] = []
        """Every duration passed to :meth:`sleep`, in request order."""

    def now(self) -> float:
        """The current virtual time."""
        return self.at

    async def sleep(self, seconds: float) -> None:
        """Wait until the virtual time is ``seconds`` later; other tasks run meanwhile.

        Raises:
            VirtualTimeLimitError: the wait would end past ``limit``.
        """
        if self.limit is not None and self.at + seconds > self.limit:
            message = f"virtual time passed its limit of {self.limit:g} s (now {self.at:g} s)"
            raise VirtualTimeLimitError(message)
        self.sleeps.append(seconds)
        # test-isolation: this sleep runs on the virtual loop, whose time is this clock, so it never waits on the wall clock
        await asyncio.sleep(seconds)


def current_virtual_clock() -> VirtualClock:
    """The clock of the virtual loop the caller is running on.

    Raises:
        RuntimeError: the caller is not running on a loop started by :func:`run_virtual`.
    """
    loop = asyncio.get_running_loop()
    if not isinstance(loop, _VirtualLoop):
        message = "this code is not running on a virtual loop (see run_virtual)"
        raise RuntimeError(message)  # noqa: TRY004  # LW-163811 [TRY004]; a missing precondition of the call, not a bad argument type.
    return loop.clock


def run_virtual[T](
    clock: VirtualClock,
    main: Coroutine[object, object, T],
    *,
    trace: EventTrace | None = None,
    schedule_seed: int | None = None,
) -> T:
    """Run ``main`` to completion on a loop whose time is ``clock``.

    Tasks still pending when ``main`` returns are cancelled and awaited, as
    ``asyncio.run`` does. When ``trace`` is given, the clock advances and the
    scheduling order of every task are recorded in it. With ``schedule_seed`` set, work that
    is ready at the same time runs in an order drawn from that seed (the same seed always
    gives the same order); without it, ties run in the order they were started.
    """
    with asyncio.Runner(loop_factory=lambda: _VirtualLoop(clock, trace, schedule_seed)) as runner:
        return runner.run(main)
