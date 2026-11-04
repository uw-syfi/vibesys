"""A scripted :class:`~vs_sim.processes.PipedProcess` whose pipes and leader a test drives.

The leader's exit and the pipes' end of file are separate events, as on a real host: a
descendant can keep a pipe open after the leader exited, and a reaped leader's group can
no longer be signalled.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

from vs_sim.processes import ProcessTimeoutError

if TYPE_CHECKING:
    from vs_sim.concurrency import Condition, Threads

_SIGTERM_STATUS = -15
_SIGKILL_STATUS = -9


class _HeldPipe(io.TextIOBase):
    """A text pipe that blocks for more output until the writer closes it."""

    def __init__(self, condition: Condition) -> None:
        self._condition = condition
        self._buffer = ""
        self._ended = False

    def readable(self) -> bool:
        return True

    def feed(self, text: str) -> None:
        with self._condition:
            if self._ended:
                message = "the pipe was already closed by its writers"
                raise RuntimeError(message)
            self._buffer += text
            self._condition.notify_all()

    def end(self) -> None:
        with self._condition:
            self._ended = True
            self._condition.notify_all()

    def read(self, size: int | None = -1) -> str:
        with self._condition:
            if size is None or size < 0:
                self._condition.wait_for(lambda: self._ended)
                text, self._buffer = self._buffer, ""
                return text
            self._condition.wait_for(lambda: self._ended or bool(self._buffer))
            text, self._buffer = self._buffer[:size], self._buffer[size:]
            return text

    def readline(self, size: int | None = -1) -> str:
        with self._condition:
            self._condition.wait_for(lambda: self._ended or "\n" in self._buffer)
            end = self._buffer.find("\n") + 1 or len(self._buffer)
            if size is not None and size >= 0:
                end = min(end, size)
            text, self._buffer = self._buffer[:end], self._buffer[end:]
            return text


class ScriptedPipedProcess:
    """A piped process group whose output, exit, and pipe closing the test scripts.

    ``write`` adds output; ``exit`` ends the leader, and by default also closes the
    pipes (no descendant held them). ``close_output`` closes them separately, for a
    descendant that outlived the leader. A signal reaches the group only until a
    ``wait`` has reaped the leader; later ones are recorded in ``refused``. Unless
    ``survives_terminate``, a ``SIGTERM`` ends the leader and closes the pipes; a
    ``SIGKILL`` always does.
    """

    def __init__(
        self,
        threads: Threads,
        *,
        merge_stderr: bool = False,
        survives_terminate: bool = False,
        wait_failure: OSError | None = None,
    ) -> None:
        """Start the group running, with empty open pipes.

        With *wait_failure*, ``wait`` raises it at once and never reports an exit, as a real
        group does when the host cannot watch its leader exit; signals still reach the group.
        """
        self._condition = threads.condition()
        self._stdout = _HeldPipe(self._condition)
        self._stderr = _HeldPipe(self._condition)
        self._merge_stderr = merge_stderr
        self._survives_terminate = survives_terminate
        self._wait_failure = wait_failure
        self._status: int | None = None
        self._reaped = False
        self.signals: list[str] = []
        self.refused: list[str] = []
        if merge_stderr:
            self._stderr.end()

    @property
    def stdout(self) -> io.TextIOBase:
        """The stdout pipe (stdout and stderr when merged)."""
        return self._stdout

    @property
    def stderr(self) -> io.TextIOBase:
        """The stderr pipe, at end of file from the start when merged."""
        return self._stderr

    @property
    def reaped(self) -> bool:
        """Whether a ``wait`` has returned the leader's status."""
        return self._reaped

    def write(self, stdout: str = "", stderr: str = "") -> None:
        """Make the group write *stdout* and *stderr* (to stdout when merged)."""
        if stdout:
            self._stdout.feed(stdout)
        if stderr:
            (self._stdout if self._merge_stderr else self._stderr).feed(stderr)

    def exit(self, status: int, *, close_output: bool = True) -> None:
        """End the leader with *status*; keep the pipes open when not *close_output*."""
        with self._condition:
            if self._status is None:
                self._status = status
            self._condition.notify_all()
        if close_output:
            self.close_output()

    def close_output(self) -> None:
        """Close both pipes, as when the last process holding them exits."""
        self._stdout.end()
        self._stderr.end()

    def wait(self, timeout: float | None) -> int:
        """Wait for the leader and reap it, raising :class:`ProcessTimeoutError` on timeout."""
        if self._wait_failure is not None:
            raise self._wait_failure
        with self._condition:
            self._condition.wait_for(lambda: self._status is not None, timeout)
            status = self._status
            if status is None:
                raise ProcessTimeoutError(("scripted",), timeout or 0.0)
            self._reaped = True
            return status

    def terminate_group(self) -> None:
        """Send ``SIGTERM`` to the group if its leader is unreaped."""
        if self._signal("terminate") and not self._survives_terminate:
            self.exit(_SIGTERM_STATUS)

    def kill_group(self) -> None:
        """Send ``SIGKILL`` to the group if its leader is unreaped."""
        if self._signal("kill"):
            self.exit(_SIGKILL_STATUS)

    def cancel_output(self) -> None:
        """Make reads return at once: the pipes reach end of file."""
        self.close_output()

    def _signal(self, name: str) -> bool:
        with self._condition:
            if self._reaped:
                self.refused.append(name)
                return False
            self.signals.append(name)
            return True
