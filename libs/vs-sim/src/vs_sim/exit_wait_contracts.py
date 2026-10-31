"""Contract suite for :class:`~vs_sim.exit_wait.ExitWaiter`.

Subclass :class:`ExitWaiterContract`, name the subclass ``Test<Variant>`` and implement
:meth:`~ExitWaiterContract.waiter`. Every real mechanism passes the same cases against real
children, so a host's choice among them changes nothing a caller can see. Cases wait on what
they observe (a child's exit), never on a duration; each child ends itself after
``HANG_GUARD_S`` so a wait that does not return fails its case instead of hanging it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

from vs_sim.waits import HANG_GUARD_S, join_or_fail

if TYPE_CHECKING:
    from vs_sim.exit_wait import ExitWaiter

# Prints ``ready``, blocks until its stdin closes, then ends as ``argv[1]`` says: an exit with
# the status ``argv[2]``, or the death by the signal numbered ``argv[2]``.
_CHILD = (
    "import os, signal, sys\n"
    f"signal.alarm({int(HANG_GUARD_S)})\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
    "if sys.argv[1] == 'signal':\n"
    "    os.kill(os.getpid(), int(sys.argv[2]))\n"
    "    signal.pause()\n"
    "sys.exit(int(sys.argv[2]))\n"
)

ENDINGS: tuple[tuple[str, int], ...] = (
    ("exit", 0),
    ("exit", 1),
    ("exit", 255),
    ("signal", signal.SIGTERM),
    ("signal", signal.SIGKILL),
)
"""How the contract's children end: ``("exit", status)`` or ``("signal", number)``."""


def start_child(ending: tuple[str, int]) -> subprocess.Popen[str]:
    """Start a child that waits for its stdin to close and then ends as *ending* says."""
    how, value = ending
    child = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-731046 [S603]; the contract starts its own fixed interpreter and script, without a shell.
        (sys.executable, "-I", "-c", _CHILD, how, str(value)),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    assert child.stdout.readline() == "ready\n"
    return child


def release(child: subprocess.Popen[str]) -> None:
    """Close the child's stdin, so it goes on to end."""
    assert child.stdin is not None
    child.stdin.close()


def _close(child: subprocess.Popen[str]) -> None:
    """Close the pipes the child's parent end still holds."""
    for stream in (child.stdin, child.stdout):
        if stream is not None:
            stream.close()


def _watch_in_thread(waiter: ExitWaiter, pid: int, order: list[str]) -> threading.Thread:
    """Wait for *pid* on another thread and note in *order* that the wait returned."""

    def wait_then_note() -> None:
        waiter.wait(pid)
        order.append("the wait returned")

    thread = threading.Thread(target=wait_then_note, name="exit-wait-contract", daemon=True)
    thread.start()
    return thread


def expected_exit_code(ending: tuple[str, int]) -> int:
    """The code ``os.waitstatus_to_exitcode`` reports for *ending*: negative for a signal."""
    how, value = ending
    return value if how == "exit" else -value


class ExitWaiterContract:
    """Cases for :class:`~vs_sim.exit_wait.ExitWaiter`."""

    def waiter(self) -> ExitWaiter:
        """A fresh waiter."""
        raise NotImplementedError

    def test_it_returns_after_the_child_ends_and_leaves_its_status_to_reap(self) -> None:
        """For every way a child can end: the wait returns once it did, and reaping still works."""
        for ending in ENDINGS:
            child = start_child(ending)
            order: list[str] = []
            watcher = _watch_in_thread(self.waiter(), child.pid, order)

            order.append("the child was released")
            release(child)
            join_or_fail(watcher)

            assert order == ["the child was released", "the wait returned"], ending
            _, status = os.waitpid(child.pid, 0)
            assert os.waitstatus_to_exitcode(status) == expected_exit_code(ending), ending
            _close(child)

    def test_it_returns_at_once_for_a_child_that_ended_earlier_and_is_not_reaped(self) -> None:
        """An exit that came before the wait is still an exit: the unreaped zombie answers."""
        for ending in ENDINGS:
            child = start_child(ending)
            release(child)
            assert child.stdout is not None
            assert child.stdout.read() == ""  # Its output closed, so it is ending or ended.

            self.waiter().wait(child.pid)

            _, status = os.waitpid(child.pid, 0)
            assert os.waitstatus_to_exitcode(status) == expected_exit_code(ending), ending
            _close(child)

    def test_a_child_that_was_already_reaped_is_not_a_child_of_this_process(self) -> None:
        """Once reaped, nothing is left to wait for: the wait says so instead of blocking."""
        child = start_child(("exit", 0))
        release(child)
        assert child.wait() == 0
        _close(child)

        try:
            self.waiter().wait(child.pid)
        except ChildProcessError:
            return
        message = "waiting for a reaped child returned instead of raising ChildProcessError"
        raise AssertionError(message)
