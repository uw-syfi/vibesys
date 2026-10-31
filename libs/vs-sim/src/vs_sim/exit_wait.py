"""Waiting for a child to exit without reaping it, by the mechanism the host provides.

A process group's owner must see the leader exit before it reaps the leader: reaping frees
the group ID for reuse, so a signal sent afterwards could reach an unrelated group. Each
mechanism here blocks on the kernel's report of the exit and leaves the zombie for the
owner. Which one a host has is a question about its capabilities (does ``os`` provide
``waitid``? does ``select`` provide ``kqueue``?), never about its name: this module is the
only place that probes the platform-limited calls, and the type checker, which knows which
platforms lack each one, rejects them anywhere else.
"""

from __future__ import annotations

import errno
import os
import select
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class ExitWaiter(Protocol):
    """Blocks until a child has exited and leaves it for its parent to reap."""

    def wait(self, pid: int) -> None:
        """Return once the child ``pid`` has exited, with its exit status still unreaped.

        Returns at once for a child that exited earlier and is still unreaped. Raises
        ``ChildProcessError`` when ``pid`` is not an unreaped child of this process, and
        ``OSError`` when the host cannot wait for it.
        """
        ...


class ExitWaitUnsupportedError(OSError):
    """The host provides no way to wait for a child to exit without reaping it."""


class _Waitid(Protocol):
    def __call__(self, idtype: int, ident: int, options: int, /) -> object: ...


class WaitidExitWaiter:
    """``waitid`` with ``WNOWAIT``: wherever the interpreter's ``os`` provides it, as on Linux."""

    def __init__(self) -> None:
        """Probe ``os`` for ``waitid``; raises ``ExitWaitUnsupportedError`` without it."""
        waitid: _Waitid | None = getattr(os, "waitid", None)
        if waitid is None:
            message = "this interpreter's os module has no waitid"
            raise ExitWaitUnsupportedError(message)
        self._waitid = waitid
        self._options = os.WEXITED | os.WNOWAIT

    def wait(self, pid: int) -> None:
        """Block in ``waitid`` until ``pid`` has exited, without reaping it."""
        self._waitid(os.P_PID, pid, self._options)


class _Kevent(Protocol):
    @property
    def flags(self) -> int: ...

    @property
    def data(self) -> int: ...


class _Kqueue(Protocol):
    def control(
        self, changelist: list[object] | None, max_events: int, timeout: float | None = None, /
    ) -> list[_Kevent]: ...

    def close(self) -> None: ...


class _KqueueFactory(Protocol):
    def __call__(self) -> _Kqueue: ...


class _KeventFactory(Protocol):
    def __call__(self, ident: int, event_filter: int, flags: int, fflags: int, /) -> object: ...


@dataclass(frozen=True)
class _KqueueCalls:
    """The kqueue calls and constants the host's ``select`` provides, probed together."""

    kqueue: _KqueueFactory
    kevent: _KeventFactory
    process_filter: int
    register: int
    one_shot: int
    error: int
    exited: int


class KqueueExitWaiter:
    """A kqueue ``NOTE_EXIT`` watch: wherever ``select`` provides ``kqueue``, as on macOS.

    The kernel posts ``NOTE_EXIT`` as the process exits, whoever reaps it, and a kqueue
    watch never reaps. A watch on a zombie is refused with ``ESRCH``, the same refusal as
    for a process that is gone, so that case is told apart with a signal-0 probe, which a
    zombie still answers.
    """

    def __init__(self) -> None:
        """Probe ``select`` for kqueue; raises ``ExitWaitUnsupportedError`` without it."""
        kqueue: _KqueueFactory | None = getattr(select, "kqueue", None)
        kevent: _KeventFactory | None = getattr(select, "kevent", None)
        process_filter = _constant("KQ_FILTER_PROC")
        register = _constant("KQ_EV_ADD")
        one_shot = _constant("KQ_EV_ONESHOT")
        error = _constant("KQ_EV_ERROR")
        exited = _constant("KQ_NOTE_EXIT")
        if (
            kqueue is None
            or kevent is None
            or process_filter is None
            or register is None
            or one_shot is None
            or error is None
            or exited is None
        ):
            message = "this interpreter's select module has no kqueue process watch"
            raise ExitWaitUnsupportedError(message)
        self._calls = _KqueueCalls(
            kqueue, kevent, process_filter, register, one_shot, error, exited
        )

    def wait(self, pid: int) -> None:
        """Block on a ``NOTE_EXIT`` watch of ``pid`` until it exits, without reaping it."""
        calls = self._calls
        queue = calls.kqueue()
        try:
            watch = calls.kevent(
                pid,
                calls.process_filter,
                calls.register | calls.one_shot,
                calls.exited,
            )
            events = queue.control([watch], 1, None)
        finally:
            queue.close()
        if not events:
            message = f"the exit watch of process {pid} ended without an event"
            raise OSError(message)
        if events[0].flags & calls.error:
            _settle_refused_watch(pid, events[0].data)


def _constant(name: str) -> int | None:
    """The integer ``select.<name>``, or ``None`` when the host's ``select`` lacks it."""
    value = getattr(select, name, None)
    return value if isinstance(value, int) else None


def _settle_refused_watch(pid: int, reason: int) -> None:
    """Return for a zombie, raise for a process that is gone or a watch that failed."""
    if reason != errno.ESRCH:
        raise OSError(reason, os.strerror(reason), pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        raise ChildProcessError(errno.ECHILD, os.strerror(errno.ECHILD), pid) from None
    except PermissionError:
        return  # A process this user cannot signal still exists, so it is not a reaped child.


class UnavailableExitWaiter:
    """The waiter of a host with no mechanism: waiting fails, naming what was probed."""

    def __init__(self, reasons: tuple[ExitWaitUnsupportedError, ...]) -> None:
        """Keep why each mechanism was unavailable."""
        self._reasons = reasons

    def wait(self, pid: int) -> None:
        """Raise ``ExitWaitUnsupportedError``: ``pid`` cannot be waited for without reaping."""
        detail = "; ".join(str(reason) for reason in self._reasons)
        message = f"cannot wait for process {pid} to exit without reaping it: {detail}"
        raise ExitWaitUnsupportedError(message)


MECHANISMS: tuple[Callable[[], ExitWaiter], ...] = (WaitidExitWaiter, KqueueExitWaiter)
"""The mechanisms :func:`host_exit_waiter` tries, in order."""


def host_exit_waiter(mechanisms: Sequence[Callable[[], ExitWaiter]] = MECHANISMS) -> ExitWaiter:
    """The first of *mechanisms* this host provides: by default ``waitid``, then a kqueue watch.

    A mechanism the host lacks raises :class:`ExitWaitUnsupportedError` when built. A host
    with none gets an :class:`UnavailableExitWaiter`, so the failure surfaces at the first
    wait that needs it and names every capability that was missing.
    """
    reasons: list[ExitWaitUnsupportedError] = []
    for mechanism in mechanisms:
        try:
            return mechanism()
        except ExitWaitUnsupportedError as unavailable:
            reasons.append(unavailable)
    return UnavailableExitWaiter(tuple(reasons))
