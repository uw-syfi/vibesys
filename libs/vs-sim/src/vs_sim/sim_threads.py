"""A cooperative, seeded simulator of threads, locks, conditions, events and sleeps.

Each simulated thread is a real operating-system thread, but exactly one of them runs at
a time: the one holding the baton. A thread gives the baton up only when it cannot go on
(it waits for a lock, a condition, an event, a join or a timer, or it ends) and, under a
schedule seed, at every synchronization point (acquire, release, notify, set, spawn,
join, sleep), where the seed may hand the baton to another runnable thread instead. The
choice of who runs next is drawn from the seed, so one seed always gives one interleaving
and different seeds explore different ones; without a seed the order is first-in,
first-out and a thread runs until it blocks.

Time is a :class:`~vs_sim.virtual.VirtualClock`. It stands still while any thread can
run and jumps to the earliest timer (a sleep or a wait with a timeout) once none can, so
waiting costs no wall time and no outcome depends on how fast the machine is. When
nothing can run and no timer is pending, the run is deadlocked: every blocked thread is
woken with :class:`SimDeadlockError` and the run fails with a message naming what each
one waited on, instead of hanging.

Plain Python between two synchronization points is atomic here, so this finds ordering
bugs around locks, conditions, events and timers, not unsynchronized data races.

The simulator composes with the virtual event loop. Passed to
:func:`~vs_sim.virtual.run_virtual`, it runs whenever the loop is idle, on the loop's own
timeline, so a blocking call run from a coroutine through :class:`SimBlockingRunner` is a
simulated thread.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import heapq
import itertools
import threading
import weakref
from collections import deque
from typing import TYPE_CHECKING, Self, cast

from vs_sim.concurrency import OsThreadLocal
from vs_sim.randomness import SeededRandom
from vs_sim.virtual import (
    SIM_MONOTONIC_ORIGIN,
    VirtualClock,
    VirtualTimeLimitError,
    current_virtual_loop,
    run_hosted,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from types import TracebackType

    from vs_sim.concurrency import Lock, ThreadLocal
    from vs_sim.trace import EventTrace


class SimDeadlockError(RuntimeError):
    """No thread can run and no timer is pending, yet some thread still waits."""


class SimMisuseError(RuntimeError):
    """A simulator was used in a way whose outcome would depend on the real scheduler."""


class _Killed(BaseException):
    """Raised inside a parked thread to unwind it when the run ends."""


class _OutsideThread:
    """Stands for a caller that is not a simulated thread (a test or the event loop)."""

    name = "<outside>"


_OUTSIDE = _OutsideThread()

_CREATED: list[weakref.ref[SimThreads]] = []
"""Every simulator built since the last :func:`undriven_threads` call."""


def undriven_threads() -> tuple[str, ...]:
    """Names of threads spawned on simulators that nothing drove, since the last call.

    A thread spawned on a simulator that no :meth:`SimThreads.run` or virtual loop drives
    never runs, so a test that relies on it passes whatever the thread would have done.
    The test harness calls this after each test; each such simulator is closed here.
    """
    made = [ref() for ref in _CREATED]
    _CREATED.clear()
    names: list[str] = []
    for threads in made:
        if threads is None:
            continue
        names.extend(threads.never_ran())
        threads.close()
    return tuple(names)


def loop_threads() -> SimThreads:
    """The simulated threads of the virtual loop the caller runs on, created on first use.

    Code that runs on a loop started by :func:`~vs_sim.virtual.run_virtual` without a
    ``driver`` can still hand blocking work to simulated threads: the loop adopts the
    returned simulator as its driver, on the loop's clock, schedule seed and trace, and
    closes it when the run ends. Every call on one loop returns the same object.

    Raises:
        RuntimeError: the caller is not on a virtual loop, or the loop's driver is not a
            :class:`SimThreads`.
    """
    loop = current_virtual_loop()
    if loop.driver is None:
        loop.adopt(SimThreads(loop.clock, schedule_seed=loop.schedule_seed, trace=loop.trace))
    if not isinstance(loop.driver, SimThreads):
        message = f"the virtual loop is driven by {type(loop.driver).__name__}, not SimThreads"
        raise RuntimeError(message)  # noqa: TRY004  # LW-163851 [TRY004]; a missing precondition of the call, not a bad argument type.
    return loop.driver


class _SimThread:
    def __init__(self, name: str, target: Callable[[], object], *, daemon: bool) -> None:
        self.name = name
        self.daemon = daemon
        self.target = target
        self.go = threading.Semaphore(0)
        self.done = False
        self.error: BaseException | None = None
        self.blocked_on: str | None = None
        self.signalled = False
        self.timer_token = 0
        self.joiners: list[_SimThread] = []
        self.poison: BaseException | None = None
        self.waiting_in: list[_SimThread] | None = None


class SimWorker:
    """The handle of a simulated thread."""

    def __init__(self, sim: SimThreads, thread: _SimThread) -> None:
        """Wrap ``thread``."""
        self._sim = sim
        self._thread = thread

    @property
    def name(self) -> str:
        """The name the thread was spawned with."""
        return self._thread.name

    def is_alive(self) -> bool:
        """Whether the target has not finished."""
        return not self._thread.done

    def join(self, timeout: float | None = None) -> None:
        """Block until the thread ends or ``timeout`` virtual seconds pass."""
        sim = self._sim
        me = sim.caller()
        sim.switch_point(me)
        if self._thread.done:
            return
        sim.block(me, f"join of {self._thread.name}", self._thread.joiners, timeout)


class SimLock:
    """A simulated lock; ``reentrant`` makes it an ``RLock``."""

    def __init__(self, sim: SimThreads, *, reentrant: bool) -> None:
        """Create an unlocked lock."""
        self.sim = sim
        self._reentrant = reentrant
        self._owner: _SimThread | _OutsideThread | None = None
        self._depth = 0
        self._waiters: list[_SimThread] = []

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:  # noqa: FBT001, FBT002  # LW-163902 [FBT001, FBT002]; the signature of ``threading.Lock.acquire``, which this class mirrors.
        """Take the lock, waiting for it when ``blocking``; ``False`` when it was not obtained."""
        sim = self.sim
        me = sim.caller()
        sim.switch_point(me)
        if self._owner is None:
            self._owner, self._depth = me, 1
            return True
        if self._reentrant and self._owner is me:
            self._depth += 1
            return True
        if not blocking or timeout == 0:
            return False
        # Release hands the lock to the first waiter, so waking normally means owning it.
        return sim.block(me, "a lock", self._waiters, None if timeout < 0 else timeout)

    def release(self) -> None:
        """Give the lock up (one level, for a reentrant lock)."""
        sim = self.sim
        me = sim.caller()
        if self._owner is not me:
            message = "cannot release a lock that this thread does not hold"
            raise RuntimeError(message)
        self._depth -= 1
        if self._depth == 0:
            self._hand_off()
        sim.switch_point(me)

    def _hand_off(self) -> None:
        if self._waiters:
            waiter = self._waiters.pop(0)
            self._owner, self._depth = waiter, 1
            self.sim.wake(waiter)
        else:
            self._owner = None

    def held_by_caller(self) -> bool:
        """Whether the calling thread holds the lock."""
        return self._owner is self.sim.caller()

    def release_all(self) -> int:
        """Release every level the caller holds; the number of levels, for ``restore``."""
        depth = self._depth
        self._depth = 0
        self._hand_off()
        return depth

    def restore(self, depth: int) -> None:
        """Take the lock again at ``depth`` levels, waiting for it."""
        sim = self.sim
        me = sim.caller()
        if self._owner is None:
            self._owner, self._depth = me, depth
            return
        sim.block(me, "a lock", self._waiters, None)
        self._depth = depth

    def __enter__(self) -> Self:
        """Acquire."""
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release."""
        self.release()


class SimCondition:
    """A simulated condition variable over a simulated lock."""

    def __init__(self, sim: SimThreads, lock: SimLock) -> None:
        """Create a condition with no waiters."""
        self._sim = sim
        self._lock = lock
        self._waiters: list[_SimThread] = []

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:  # noqa: FBT001, FBT002  # LW-163903 [FBT001, FBT002]; the signature of ``threading.Condition.acquire``, which this class mirrors.
        """Acquire the underlying lock."""
        return self._lock.acquire(blocking, timeout)

    def release(self) -> None:
        """Release the underlying lock."""
        self._lock.release()

    def __enter__(self) -> Self:
        """Acquire the underlying lock."""
        self._lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Release the underlying lock."""
        self._lock.release()

    def _require_held(self, action: str) -> None:
        if not self._lock.held_by_caller():
            message = f"cannot {action} an un-acquired lock"
            raise RuntimeError(message)

    def wait(self, timeout: float | None = None) -> bool:
        """Release the lock and wait for a notification; ``False`` on timeout. The lock is held on return."""
        self._require_held("wait on")
        sim = self._sim
        me = sim.caller()
        depth = self._lock.release_all()
        try:
            return sim.block(me, "a condition", self._waiters, timeout)
        finally:
            self._lock.restore(depth)

    def wait_for(self, predicate: Callable[[], bool], timeout: float | None = None) -> bool:
        """Wait until ``predicate()`` holds; its last value."""
        end: float | None = None
        remaining = timeout
        result = predicate()
        while not result:
            if remaining is not None:
                if end is None:
                    end = self._sim.now() + remaining
                else:
                    remaining = end - self._sim.now()
                    if remaining <= 0:
                        break
            self.wait(remaining)
            result = predicate()
        return result

    def notify(self, n: int = 1) -> None:
        """Wake up to ``n`` waiters, longest-waiting first."""
        self._require_held("notify on")
        for _ in range(min(n, len(self._waiters))):
            self._sim.wake(self._waiters.pop(0))

    def notify_all(self) -> None:
        """Wake every waiter."""
        self.notify(len(self._waiters))


class SimEvent:
    """A simulated event flag."""

    def __init__(self, sim: SimThreads) -> None:
        """Create a cleared event."""
        self._sim = sim
        self._flag = False
        self._waiters: list[_SimThread] = []

    def is_set(self) -> bool:
        """Whether the flag is set."""
        return self._flag

    def set(self) -> None:
        """Set the flag and wake every waiter."""
        self._flag = True
        while self._waiters:
            self._sim.wake(self._waiters.pop(0))
        self._sim.switch_point(self._sim.caller())

    def clear(self) -> None:
        """Clear the flag."""
        self._flag = False

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the flag; its value on return."""
        sim = self._sim
        me = sim.caller()
        sim.switch_point(me)
        if not self._flag:
            sim.block(me, "an event", self._waiters, timeout)
        return self._flag


class SimThreads:
    """Threads, locks, conditions, events and sleeps run one at a time in a seeded order.

    Build one per simulation, give it the run's :class:`VirtualClock` (it makes one when
    omitted) and its schedule seed, then either call :meth:`run` with a plain function or
    pass the object to :func:`~vs_sim.virtual.run_virtual` and spawn from coroutines.
    """

    def __init__(
        self,
        clock: VirtualClock | None = None,
        *,
        schedule_seed: int | None = None,
        trace: EventTrace | None = None,
    ) -> None:
        """Start with no threads, on ``clock`` (a new one when omitted)."""
        self.clock = clock or VirtualClock()
        self._rng = None if schedule_seed is None else SeededRandom(schedule_seed)
        self._trace = trace
        self._runnable: deque[_SimThread] = deque()
        self._timers: list[tuple[float, int, _SimThread, int]] = []
        self._sequence = itertools.count()
        self._threads: list[_SimThread] = []
        self._by_ident: dict[int, _SimThread] = {}
        self._yield_to_driver = threading.Semaphore(0)
        self._closed = False
        self._driven = False
        self._outside_ident = threading.get_ident()
        self._executor = _SimExecutor(self)
        self.errors: list[BaseException] = []
        """Exceptions that ended threads, in the order they happened."""
        _CREATED.append(weakref.ref(self))

    # ----- the Threads interface -----

    def spawn(self, target: Callable[[], object], *, name: str, daemon: bool = True) -> SimWorker:
        """Start ``target`` as a simulated thread; it first runs when the baton reaches it."""
        me = self.caller()
        thread = self._spawn(target, name, daemon=daemon)
        self.switch_point(me)
        return SimWorker(self, thread)

    def _spawn(self, target: Callable[[], object], name: str, *, daemon: bool) -> _SimThread:
        thread = _SimThread(name, target, daemon=daemon)
        self._threads.append(thread)
        os_thread = threading.Thread(target=self._entry, args=(thread,), name=name, daemon=True)
        os_thread.start()
        self._runnable.append(thread)
        return thread

    def lock(self) -> SimLock:
        """A new non-reentrant lock."""
        return SimLock(self, reentrant=False)

    def rlock(self) -> SimLock:
        """A new reentrant lock."""
        return SimLock(self, reentrant=True)

    def condition(self, lock: Lock | None = None) -> SimCondition:
        """A new condition over ``lock``, which must come from this simulator."""
        if lock is None:
            lock = self.rlock()
        if not isinstance(lock, SimLock) or lock.sim is not self:
            message = "a simulated condition needs a lock from the same SimThreads"
            raise TypeError(message)
        return SimCondition(self, lock)

    def event(self) -> SimEvent:
        """A new, cleared event."""
        return SimEvent(self)

    def local[T](self, initial: Callable[[], T]) -> ThreadLocal[T]:
        """A per-thread value; each simulated thread runs on an OS thread of its own."""
        return OsThreadLocal(initial)

    def sleep(self, seconds: float) -> None:
        """Block the calling simulated thread for ``seconds`` of virtual time."""
        me = self.caller()
        self.switch_point(me)
        if seconds > 0:
            self.block(me, f"sleep({seconds:g})", None, seconds)

    def now(self) -> float:
        """Monotonic virtual seconds: the timeline plus ``SIM_MONOTONIC_ORIGIN``.

        The offset keeps this reading apart from the timeline's own clock and from
        ``WallClockView``, so code that compares a monotonic reading with a clock
        reading is visibly wrong in simulation, as it is in production.
        """
        return SIM_MONOTONIC_ORIGIN + self.clock.now()

    def run_async[T](self, main: Coroutine[object, object, T]) -> T:
        """Run ``main`` to completion on an event loop on the calling simulated thread.

        The loop's time is this simulation's timeline. While it has nothing to do, its
        thread waits like any other simulated thread, so the others run and time advances
        to the earliest timer, the loop's own or a thread's; a callback another simulated
        thread schedules on it (``call_soon_threadsafe``, as ``SimBlockingRunner`` does)
        wakes it.

        Raises:
            RuntimeError: the caller is not a simulated thread.
        """
        if isinstance(self.caller(), _OutsideThread):
            message = "run_async must be called from a simulated thread"
            raise RuntimeError(message)  # noqa: TRY004  # LW-222587 [TRY004]; a missing precondition of the call, not a bad argument type.
            # > TypeError would say an argument has the wrong type; no argument is wrong here.
        seed = None if self._rng is None else self._rng.randint(0, 2**32 - 1)
        return run_hosted(
            self.clock, main, host=_LoopWake(self), trace=self._trace, schedule_seed=seed
        )

    # ----- driving -----

    def run[T](self, main: Callable[[], T]) -> T:
        """Run ``main`` as the first thread until it and every non-daemon thread have ended.

        Raises:
            SimDeadlockError: nothing can run and no timer is pending.
            VirtualTimeLimitError: the next timer is past the clock's ``limit``, so the run is
                stuck rather than slow.
        """
        self._driven = True
        outcome: list[T] = []
        first = self._start_main(main, outcome)
        try:
            while True:
                self.run_until_blocked()
                if first.done and all(t.done for t in self._threads if not t.daemon):
                    break
                due = self.next_timer()
                if due is None:
                    raise SimDeadlockError(self.describe())
                if self.clock.limit is not None and due > self.clock.limit:
                    message = (
                        f"virtual time passed its limit of {self.clock.limit:g} s; "
                        f"{self._waiting()}"
                    )
                    raise VirtualTimeLimitError(message)
                self.advance_to(due)
        finally:
            self.close()
        if first.error is not None:
            raise first.error
        if self.errors:
            raise self.errors[0]
        return outcome[0]

    def _start_main[T](self, main: Callable[[], T], outcome: list[T]) -> _SimThread:
        def body() -> None:
            outcome.append(main())

        return self._spawn(body, "main", daemon=False)

    def run_until_blocked(self) -> None:
        """Run runnable threads, in the seeded order, until every thread waits or has ended."""
        while self._runnable:
            thread = self._pick()
            if self._trace is not None:
                self._trace.step(f"thread:{thread.name}")
            thread.go.release()
            self._yield_to_driver.acquire()

    def next_timer(self) -> float | None:
        """The virtual time of the earliest live timer, or ``None`` when there is none."""
        while self._timers:
            due, _, thread, token = self._timers[0]
            if thread.timer_token == token and thread.blocked_on is not None:
                return due
            heapq.heappop(self._timers)
        return None

    def advance(self, seconds: float) -> None:
        """Move the clock ``seconds`` forward and wake every thread whose timer is due by then."""
        self.advance_to(self.clock.now() + max(0.0, seconds))

    def advance_to(self, instant: float) -> None:
        """Move the clock to ``instant`` and wake every thread whose timer is due by then.

        ``instant`` is on the clock's timeline (``clock.now()``), not on ``now()``'s.
        """
        if self._trace is not None and instant > self.clock.at:
            self._trace.advance(self.clock.at, instant)
        self.clock.at = max(self.clock.at, instant)
        while self._timers and self._timers[0][0] <= self.clock.at:
            _, _, thread, token = heapq.heappop(self._timers)
            if thread.timer_token != token or thread.blocked_on is None:
                continue
            if thread.waiting_in is not None:
                thread.waiting_in.remove(thread)
            self._make_runnable(thread, signalled=False)

    def describe(self) -> str:
        """What every unfinished thread waits on, for a deadlock report."""
        return "no thread can run and no timer is pending; " + self._waiting()

    def _waiting(self) -> str:
        return "; ".join(
            f"{t.name}: {t.blocked_on or 'runnable'}" for t in self._threads if not t.done
        )

    def close(self) -> None:
        """Unwind every thread still parked, so no operating-system thread outlives the run."""
        if self._closed:
            return
        self._closed = True
        for thread in [t for t in self._threads if not t.done]:
            while not thread.done:
                thread.poison = _Killed()
                thread.go.release()
                self._yield_to_driver.acquire()

    # ----- inside the scheduler -----

    def caller(self) -> _SimThread | _OutsideThread:
        """The simulated thread running this code, or the one real thread that drives them.

        Every caller that is not a simulated thread is the same stand-in, so only one real
        thread may use a simulator: the one that built it (the test's, or the event loop's
        that drives it). Two would share lock ownership and race on its state. The thread
        is fixed at construction, so a worker that uses the simulator fails on every run,
        not only on the runs where a pool happens to hand the work to a second worker.

        Raises:
            SimMisuseError: a real thread other than the one that built this simulator used it.
        """
        ident = threading.get_ident()
        thread = self._by_ident.get(ident)
        if thread is not None:
            return thread
        if ident != self._outside_ident:
            message = (
                f"real thread {threading.current_thread().name!r} used a SimThreads built on "
                "another real thread; run the work as a simulated thread (spawn it from "
                "SimThreads.run or Sim.run) instead of on a real one"
            )
            raise SimMisuseError(message)
        return _OUTSIDE

    @property
    def executor(self) -> concurrent.futures.Executor:
        """Runs each submitted call as a new simulated thread, in place of a real thread pool."""
        return self._executor

    def attach(self) -> None:
        """Mark this simulator as driven by a virtual loop from now on."""
        self._driven = True

    def never_ran(self) -> tuple[str, ...]:
        """Names of threads spawned on a simulator that nothing ever drove."""
        if self._driven:
            return ()
        return tuple(thread.name for thread in self._threads)

    def _entry(self, thread: _SimThread) -> None:
        thread.go.acquire()
        self._by_ident[threading.get_ident()] = thread
        try:
            if thread.poison is None:
                thread.target()
        except _Killed:
            pass
        except BaseException as error:  # noqa: BLE001  # LW-163912 [BLE001]; a thread's failure is recorded for the run to re-raise, as the thread has no caller to receive it.
            thread.error = error
            self.errors.append(error)
        finally:
            thread.done = True
            thread.blocked_on = None
            for joiner in thread.joiners:
                self.wake(joiner)
            thread.joiners.clear()
            del self._by_ident[threading.get_ident()]
            self._yield_to_driver.release()

    def _pick(self) -> _SimThread:
        if self._rng is None:
            return self._runnable.popleft()
        index = self._rng.randint(0, len(self._runnable) - 1)
        thread = self._runnable[index]
        del self._runnable[index]
        return thread

    def switch_point(self, me: _SimThread | _OutsideThread) -> None:
        """Let the seed hand the baton to another runnable thread."""
        if self._rng is None or isinstance(me, _OutsideThread) or not self._runnable:
            return
        if self._rng.randint(0, len(self._runnable)) == 0:
            return
        self._runnable.append(me)
        self._park(me, "a turn")

    def _make_runnable(self, thread: _SimThread, *, signalled: bool) -> None:
        thread.signalled = signalled
        thread.blocked_on = None
        thread.waiting_in = None
        thread.timer_token += 1
        self._runnable.append(thread)

    def wake(self, thread: _SimThread) -> None:
        """Wake ``thread`` because what it waited for happened."""
        self._make_runnable(thread, signalled=True)

    def block(
        self,
        me: _SimThread | _OutsideThread,
        what: str,
        waiters: list[_SimThread] | None,
        timeout: float | None,
    ) -> bool:
        """Park until woken (``True``) or until ``timeout`` virtual seconds pass (``False``)."""
        if me is _OUTSIDE:
            message = f"a thread that is not simulated cannot block on {what}"
            raise RuntimeError(message)
        me = cast("_SimThread", me)
        me.signalled = False
        me.timer_token += 1
        if waiters is not None:
            waiters.append(me)
            me.waiting_in = waiters
        if timeout is not None:
            heapq.heappush(
                self._timers,
                (self.clock.at + max(timeout, 0.0), next(self._sequence), me, me.timer_token),
            )
        self._park(me, what)
        return me.signalled

    def _park(self, me: _SimThread, what: str) -> None:
        me.blocked_on = what
        self._yield_to_driver.release()
        me.go.acquire()
        me.blocked_on = None
        if me.poison is not None:
            raise me.poison


class _LoopWake:
    """Parks the thread of a loop started by ``run_async`` until a callback or a timer."""

    def __init__(self, sim: SimThreads) -> None:
        self._sim = sim
        self._woken = sim.event()

    def park(self, timeout: float | None) -> None:
        self._woken.wait(timeout)
        self._woken.clear()

    def wake(self) -> None:
        # Only a simulated thread can hand over the baton. A thread outside the simulation
        # that schedules a callback would otherwise leave the loop asleep until its next
        # timer, so it is refused loudly.
        if isinstance(self._sim.caller(), _OutsideThread):
            message = "a thread outside the simulation cannot wake a loop started by run_async"
            raise RuntimeError(message)  # noqa: TRY004  # LW-988825 [TRY004]; the caller is outside the simulation, not an argument of the wrong type.
            # > TypeError would say an argument has the wrong type; no argument is wrong here.
        self._woken.set()

    def now(self) -> float:
        return self._sim.now()

    @property
    def executor(self) -> concurrent.futures.Executor:
        return self._sim.executor


class _SimExecutor(concurrent.futures.Executor):
    """Runs each submitted call as a new simulated thread."""

    def __init__(self, sim: SimThreads) -> None:
        self._sim = sim
        self._count = itertools.count()

    def submit[T](
        self, fn: Callable[..., T], /, *args: object, **kwargs: object
    ) -> concurrent.futures.Future[T]:
        future: concurrent.futures.Future[T] = concurrent.futures.Future()

        def call() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = fn(*args, **kwargs)
            except BaseException as error:  # noqa: BLE001  # LW-096717 [BLE001]; the future carries any failure back to its awaiter, as a thread pool's does.
                # > Catching less would kill the simulated thread and leave the awaiter hanging.
                future.set_exception(error)
            else:
                future.set_result(result)

        self._sim.spawn(call, name=f"executor-{next(self._count)}")
        return future


class SimBlockingRunner:
    """A :class:`~vs_sim.blocking.BlockingRunner` whose calls are simulated threads."""

    def __init__(self, threads: SimThreads) -> None:
        """Run calls on ``threads``."""
        self._threads = threads
        self._count = itertools.count()

    async def run[**P, T](self, function: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
        """Call ``function`` on a new simulated thread and await its result."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = loop.create_future()

        def settle(result: T | None, error: BaseException | None) -> None:
            if future.done():
                return
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(result)  # ty: ignore[invalid-argument-type]

        def body() -> None:
            try:
                result = function(*args, **kwargs)
            except BaseException as error:  # noqa: BLE001  # LW-163913 [BLE001]; the caller's exception travels to the awaiting coroutine unchanged.
                loop.call_soon_threadsafe(settle, None, error)
            else:
                loop.call_soon_threadsafe(settle, result, None)

        self._threads.spawn(body, name=f"blocking-{next(self._count)}")
        return await future
