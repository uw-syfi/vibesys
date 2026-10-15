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
import heapq
import selectors
from typing import TYPE_CHECKING, Any, Protocol, cast, overload

from vs_sim.randomness import SeededRandom

if TYPE_CHECKING:
    import concurrent.futures
    import contextvars
    from collections.abc import Callable, Coroutine, Generator
    from types import TracebackType

    from vs_sim.trace import EventTrace


WORKER_GUARD_S = 60.0
"""Real seconds a worker thread may run before the loop gives up on it; a hang guard only."""


class IdleDriver(Protocol):
    """Work that runs whenever the loop is idle and owns timers on the loop's timeline.

    :class:`~vs_sim.sim_threads.SimThreads` is the one implementation: while the loop has
    nothing to do it runs the simulated threads until they all wait, and the clock then
    jumps to whichever is due first, the loop's next timer or a thread's.
    """

    def attach(self) -> None:
        """Take note that a virtual loop drives this work from now on."""
        ...

    @property
    def executor(self) -> concurrent.futures.Executor:
        """The loop's default executor: ``run_in_executor`` calls run as this work's threads."""
        ...

    def run_until_blocked(self) -> None:
        """Run until nothing the driver owns can run."""
        ...

    def next_timer(self) -> float | None:
        """The earliest instant the driver wants the clock to reach, or ``None``."""
        ...

    def advance_to(self, instant: float) -> None:
        """Move the clock to ``instant`` and let what is due there become runnable."""
        ...

    def describe(self) -> str:
        """What the driver's work waits on, for a deadlock report."""
        ...

    def close(self) -> None:
        """Release everything the driver holds when the run ends."""
        ...


class LoopHost(Protocol):
    """The simulated thread a loop runs on, which it hands the baton back to while idle.

    :class:`~vs_sim.sim_threads.SimThreads` implements it for ``run_async``: an idle loop
    parks its thread on the simulated timeline, so the other simulated threads run and
    time advances, and a callback scheduled from another simulated thread wakes it.
    """

    def park(self, timeout: float | None) -> None:
        """Wait on the timeline until woken or ``timeout`` seconds pass; ``None`` waits for a wake."""
        ...

    def wake(self) -> None:
        """End a current or the next ``park``; raises when the caller cannot hand over the baton."""
        ...

    def now(self) -> float:
        """The reading the loop's ``time()`` returns: the host's own monotonic clock."""
        ...

    @property
    def executor(self) -> concurrent.futures.Executor:
        """The loop's default executor, which runs ``run_in_executor`` calls as host threads."""
        ...


class VirtualDeadlockError(RuntimeError):
    """Every task is waiting and no timer is scheduled: nothing can ever wake the run."""


class VirtualTimeLimitError(RuntimeError):
    """A run slept past ``VirtualClock.limit``: it is stuck, not slow, because virtual time is free."""


class _Timer(asyncio.TimerHandle):
    """A timer that, among timers due at the same instant, fires in ``rank`` order."""

    __slots__ = ("_rank",)

    def __init__(
        self,
        when: float,
        callback: Callable[..., object],
        args: tuple[object, ...],
        loop: asyncio.AbstractEventLoop,
        context: contextvars.Context | None,
    ) -> None:
        super().__init__(when, callback, args, loop, context)
        self._rank: tuple[float, int] = (0.0, 0)

    def __lt__(self, other: object) -> bool:
        if isinstance(other, _Timer):
            return (self.when(), self._rank) < (other.when(), other._rank)
        return NotImplemented

    def push(self, heap: list[asyncio.TimerHandle], rank: tuple[float, int]) -> None:
        """Enter the loop's timer heap at ``rank``, marked as scheduled so ``cancel`` tells the loop."""
        self._rank = rank
        heapq.heappush(heap, self)
        self._scheduled = True


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
        if self.loop is not None and self.loop.driver is not None:
            driven = self._drive(self.loop.driver, timeout)
            if driven is not None:
                return driven
        if self.loop is not None and self.loop.workers_in_flight > 0:
            return self._await_worker()
        if self.loop is not None and self.loop.host is not None:
            self.loop.host.park(timeout)
            return super().select(0)
        if timeout is None:
            raise VirtualDeadlockError(self._describe_waiters())
        if self._trace is not None:
            self._trace.advance(self._clock.at, self._clock.at + timeout)
        self._clock.at += timeout
        return []

    def _await_worker(self) -> list[tuple[selectors.SelectorKey, int]]:
        """Time stands still while a worker thread runs; its completion wakes this select."""
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

    def _drive(
        self, driver: IdleDriver, timeout: float | None
    ) -> list[tuple[selectors.SelectorKey, int]] | None:
        """Let the driver run, and jump to its timers that fall before the loop's own.

        Returns the ready events once something reaches the loop, or ``None`` when the
        loop's own next timer (or the lack of any work) is what comes next.
        """
        while True:
            driver.run_until_blocked()
            ready = super().select(0)
            if ready:
                if self._trace is not None:
                    self._trace.io(len(ready))
                return ready
            due = driver.next_timer()
            if due is None or (timeout is not None and self._clock.at + timeout <= due):
                return None
            driver.advance_to(due)

    def _describe_waiters(self) -> str:
        message = "every task is waiting and no timer is scheduled"
        if self.loop is None:
            return message
        if self.loop.driver is not None:
            message = f"{message}; {self.loop.driver.describe()}"
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
        self._timers_started = 0
        self.clock = clock
        self.workers_in_flight = 0
        self.driver: IdleDriver | None = None
        self.host: LoopHost | None = None
        self.schedule_seed = schedule_seed
        self.trace = trace
        if trace is not None:
            self._created = 0
            self.set_task_factory(self._recording_task)
            self._trace = trace

    def adopt(self, driver: IdleDriver) -> None:
        """Let ``driver`` run whenever this loop is idle, until the run ends."""
        self.driver = driver
        driver.attach()

    def time(self) -> float:
        return self._clock.at if self.host is None else self.host.now()

    def _write_to_self(self) -> None:
        # A callback scheduled from another thread writes to the loop's wakeup pipe; a hosted
        # loop parked on the timeline must also be woken there, or it never reads the pipe.
        super()._write_to_self()  # ty: ignore[unresolved-attribute]
        if self.host is not None:
            self.host.wake()

    def call_at(
        self,
        when: float,
        callback: Callable[..., object],
        *args: object,
        context: contextvars.Context | None = None,
    ) -> asyncio.TimerHandle:
        # asyncio keeps timers in a heap that is not stable: timers due at the same instant
        # pop in an arbitrary order, and on this loop every sleep started in one turn has
        # the same due time. Rank timers for an instant explicitly, by start order or by a
        # seeded draw, and keep the due time itself exact so the clock and every other
        # timer are untouched by the tie-break.
        self._check_closed()  # ty: ignore[unresolved-attribute]
        self._timers_started += 1
        draw = 0.0 if self._schedule is None else self._schedule.random()
        timer = _Timer(when, callback, args, self, context)
        timer.push(self._scheduled, (draw, self._timers_started))  # ty: ignore[unresolved-attribute]
        return timer

    def _run_once(self) -> None:
        # Under a schedule seed, the callbacks ready at the start of a turn run in a seeded
        # permutation. Permuting the whole batch once per turn (rather than inserting each new
        # callback at a random queue position) keeps asyncio's contract that a callback
        # scheduled in one turn runs in the next: `_run_once` runs exactly the handles queued
        # when it starts, so a handle inserted among them could push an older one out of that
        # turn, again and again, and a run that finished first never ran it.
        # `_ready` is asyncio's own queue, which this loop subclasses; the standard library's
        # queue is not in the type stubs.
        if self._schedule is not None:
            ready = self._ready  # ty: ignore[unresolved-attribute]
            if len(ready) > 1:
                batch = list(ready)
                self._schedule.shuffle(batch)
                ready.clear()
                ready.extend(batch)
        super()._run_once()  # ty: ignore[unresolved-attribute]

    def run_in_executor[*Ts, T](
        self,
        executor: concurrent.futures.Executor | None,
        func: Callable[[*Ts], T],
        *args: *Ts,
    ) -> asyncio.Future[T]:
        owner = self.host or self.driver
        if owner is not None and executor is None:
            # The owner's executor runs the call as a simulated thread, whose completion wakes
            # this loop like any other callback, and no real thread reaches the simulation's
            # locks. (asyncio accepts only a real thread pool as the default executor, so it is
            # chosen here.)
            return super().run_in_executor(owner.executor, func, *args)
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
        **task_options: object,
    ) -> asyncio.Task[object]:
        # Task names count process-wide, so label by creation order within this loop.
        label = f"{getattr(coro, '__qualname__', type(coro).__name__)}#{self._created}"
        self._created += 1
        # The event loop forwards every task option its Python version supports. Keep that
        # contract open rather than copying today's version-specific name/eager_start set.
        task_constructor = cast("Callable[..., asyncio.Task[object]]", asyncio.Task)
        return task_constructor(
            _RecordedCoroutine(coro, label, self._trace),
            loop=loop,
            context=context,
            **task_options,
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


SIM_WALL_EPOCH = 1_700_000_000.0
"""Where a simulated wall clock starts: 2023-11-14 UTC."""

SIM_MONOTONIC_ORIGIN = 1_000_000.0
"""Where simulated monotonic time (``SimThreads.now``) starts, like seconds since a boot.

It differs from both the timeline (near zero) and ``SIM_WALL_EPOCH``, so a reading of one
compared with another is off by days or decades in every simulated test.
"""


class WallClockView:
    """A simulated timeline read as wall-clock time: the same ticks, offset by an epoch.

    The timeline itself (``VirtualClock.now``) starts near zero and ``SimThreads.now`` near
    ``SIM_MONOTONIC_ORIGIN``. This view is the wall-clock reading of the same timeline, so a
    timestamp and a monotonic reading never agree by accident.
    """

    def __init__(self, timeline: VirtualClock, epoch: float = SIM_WALL_EPOCH) -> None:
        """Read ``timeline`` as seconds since the Unix epoch, starting near ``epoch``."""
        self._timeline = timeline
        self._epoch = epoch

    def now(self) -> float:
        """Seconds since the Unix epoch on the simulated timeline."""
        return self._epoch + self._timeline.now()

    async def sleep(self, seconds: float) -> None:
        """Sleep on the underlying timeline."""
        await self._timeline.sleep(seconds)


def current_virtual_loop() -> _VirtualLoop:
    """The virtual loop the caller is running on.

    Raises:
        RuntimeError: the caller is not running on a loop started by :func:`run_virtual`.
    """
    loop = asyncio.get_running_loop()
    if not isinstance(loop, _VirtualLoop):
        message = "this code is not running on a virtual loop (see run_virtual)"
        raise RuntimeError(message)  # noqa: TRY004  # LW-163811 [TRY004]; a missing precondition of the call, not a bad argument type.
    return loop


def current_virtual_clock() -> VirtualClock:
    """The clock of the virtual loop the caller is running on.

    Raises:
        RuntimeError: the caller is not running on a loop started by :func:`run_virtual`.
    """
    return current_virtual_loop().clock


def run_virtual[T](
    clock: VirtualClock,
    main: Coroutine[object, object, T],
    *,
    trace: EventTrace | None = None,
    schedule_seed: int | None = None,
    driver: IdleDriver | None = None,
) -> T:
    """Run ``main`` to completion on a loop whose time is ``clock``.

    Tasks still pending when ``main`` returns are cancelled and awaited, as
    ``asyncio.run`` does. When ``trace`` is given, the clock advances and the
    scheduling order of every task are recorded in it. With ``schedule_seed`` set, work that
    is ready at the same time runs in an order drawn from that seed (the same seed always
    gives the same order); without it, ties run in the order they were started.

    A ``driver`` (simulated threads) runs whenever the loop is idle and shares the clock;
    it is closed when the run ends. Code on the loop can also ask for one with
    :func:`~vs_sim.sim_threads.loop_threads`.
    """
    loops: list[_VirtualLoop] = []

    def make_loop() -> _VirtualLoop:
        loop = _VirtualLoop(clock, trace, schedule_seed)
        loops.append(loop)
        if driver is not None:
            loop.adopt(driver)
        return loop

    try:
        with asyncio.Runner(loop_factory=make_loop) as runner:
            return runner.run(main)
    finally:
        for loop in loops:
            if loop.driver is not None:
                loop.driver.close()


def run_hosted[T](
    clock: VirtualClock,
    main: Coroutine[object, object, T],
    *,
    host: LoopHost,
    trace: EventTrace | None = None,
    schedule_seed: int | None = None,
) -> T:
    """Run ``main`` to completion on a loop whose time is ``clock``, on ``host``'s thread.

    Unlike :func:`run_virtual`, the loop does not drive the simulated threads: it runs on
    one of them, and while idle it parks that thread until another wakes it or its next
    timer is due. Its ``time()`` is ``host.now()``, and ``run_in_executor`` (so also
    ``asyncio.to_thread``) runs calls on ``host.executor``. ``SimThreads.run_async`` is the
    caller.
    """

    def make_loop() -> _VirtualLoop:
        loop = _VirtualLoop(clock, trace, schedule_seed)
        loop.host = host
        return loop

    with asyncio.Runner(loop_factory=make_loop) as runner:
        return runner.run(main)
