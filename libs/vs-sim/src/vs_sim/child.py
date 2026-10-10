"""Run code that changes process-wide state in a child process, not in the test process.

Signals, signal handlers, the environment, the working directory, interval timers and
umask belong to the whole process. A test that changes them in the shared pytest process
(or a pytest-xdist worker) leaks into every test that runs after it, or kills the worker.
``run_in_child`` runs the function in a forked child that exits afterwards, so the state
dies with it and the test process is untouched.
"""

from __future__ import annotations

import multiprocessing
import pickle
import signal
import traceback
import warnings
from typing import TYPE_CHECKING, Any

from vs_sim.waits import HANG_GUARD_S

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection


class ChildDiedError(AssertionError):
    """The child ended without delivering a result (killed by a signal, or exited early)."""


def _describe_exit(exitcode: int | None) -> str:
    if exitcode is None:
        return "was still running"
    if exitcode < 0:
        return f"was killed by {signal.Signals(-exitcode).name}"
    return f"exited with status {exitcode}"


def _child_main(function: Callable[[], Any], channel: Connection) -> None:
    try:
        outcome: tuple[str, Any, str] = ("ok", function(), "")
    except BaseException as error:  # noqa: BLE001  # LW-163803 [BLE001]; the child reports every failure, including SystemExit, to the parent that raises it.
        outcome = ("error", error, traceback.format_exc())
    try:
        channel.send(outcome)
    except (pickle.PicklingError, TypeError, AttributeError) as unsendable:
        channel.send(("unsendable", repr(outcome[1]), f"{outcome[2]}\n{unsendable!r}"))
    finally:
        channel.close()


def run_in_child[T](function: Callable[[], T], *, timeout_s: float = HANG_GUARD_S) -> T:
    """Call ``function`` in a forked child and return its result, or raise what it raised.

    The result and any exception must be picklable. ``timeout_s`` only guards against a
    hung child, which is killed and reported; it is not what a test synchronizes on.

    Raises:
        ChildDiedError: the child was killed or exited without a result, or hung.
        BaseException: whatever ``function`` raised in the child, with the child's
            traceback attached as a note.
    """
    context = multiprocessing.get_context("fork")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(target=_child_main, args=(function, sender))
    with warnings.catch_warnings():
        # Forking while the test process has other threads is what this helper is for;
        # the child runs one function and exits, so it never needs a lock another thread held.
        warnings.simplefilter("ignore", DeprecationWarning)
        child.start()
    sender.close()
    try:
        delivered = receiver.poll(timeout_s)
        outcome = receiver.recv() if delivered else None
    except EOFError:
        outcome = None
    finally:
        receiver.close()
        if child.is_alive() and outcome is None:
            child.kill()
        child.join(timeout_s)
    if outcome is None:
        message = f"the child {_describe_exit(child.exitcode)} without delivering a result"
        raise ChildDiedError(message)
    kind, value, remote_traceback = outcome
    if kind == "ok":
        return value
    if kind == "unsendable":
        message = f"the child's outcome could not be pickled: {value}\n{remote_traceback}"
        raise ChildDiedError(message)
    value.add_note(f"in the child:\n{remote_traceback}")
    raise value
