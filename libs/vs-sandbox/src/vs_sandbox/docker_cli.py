"""The Docker CLI as a port, so a sandbox can run against a real or a fake daemon."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Protocol

from vs_sandbox.process_execution import start_process_group
from vs_sim.api import (
    ProcessGroupUnreaped,
    ProcessOutcome,
    ProcessOutputTooLarge,
    ProcessSpec,
    ProcessTimedOut,
    ProcessTimeoutError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_sim.api import Processes

# Signals every process whose environment carries the marker in ``$1`` with
# signal number ``$2``. Passed as ``sh -c SCRIPT sh MARKER SIGNAL`` so the
# marker never needs shell quoting, and so a fake container can recognise the
# stop request by this exact script instead of parsing its text.
SIGNAL_EXEC_SCRIPT = (
    "for f in /proc/[0-9]*/environ; do"
    ' if tr "\\0" "\\n" 2>/dev/null < "$f" | grep -qx -- "$1"; then'
    ' p=${f#/proc/}; kill -"$2" "${p%/environ}" 2>/dev/null; fi;'
    " done; true"
)
# Every ``execute`` tags its in-container processes with this environment
# variable so a stop can find them: stopping the ``docker exec`` client alone
# leaves the command running in the container.
EXEC_MARKER_ENV = "VIBESYS_EXEC_ID"


class DockerCli(Protocol):
    """Run ``docker`` commands on behalf of :class:`~vs_sandbox.docker_sandbox.DockerSandbox`."""

    def run(
        self, argv: Sequence[str], *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        """Run *argv* to completion with captured text streams.

        Raises ``subprocess.TimeoutExpired`` past *timeout_seconds* and
        ``OSError`` when ``docker`` cannot be launched.
        """
        ...

    def spawn(self, argv: Sequence[str]) -> subprocess.Popen[str]:
        """Start *argv* as a process group leader with piped, text-mode output."""
        ...


class SubprocessDockerCli:
    """The real ``docker`` binary on ``PATH``."""

    def __init__(self, *, processes: Processes) -> None:
        """Run synchronous Docker commands through *processes*."""
        self._processes = processes

    def run(
        self, argv: Sequence[str], *, timeout_seconds: float
    ) -> subprocess.CompletedProcess[str]:
        """Run *argv* with captured output, in a process group of its own.

        Ctrl-C signals the terminal's whole foreground process group. A
        lifecycle command (``run``, ``stop``, ``rm``) that shared it would die
        mid-flight and leave its container running or half-created, so the
        run's own teardown, not the terminal, decides when these end. A
        timeout kills and reaps the command's whole process group before
        :class:`subprocess.TimeoutExpired` reaches the caller.
        """
        arguments = list(argv)
        match self._processes.run(ProcessSpec(tuple(arguments)), timeout=timeout_seconds):
            case ProcessOutcome(returncode=returncode, stdout=stdout, stderr=stderr):
                return subprocess.CompletedProcess(
                    arguments, returncode, _text(stdout), _text(stderr)
                )
            case ProcessTimedOut(stdout=stdout, stderr=stderr):
                raise ProcessTimeoutError(
                    arguments, timeout_seconds, output=stdout or None, stderr=stderr or None
                )
            case ProcessGroupUnreaped(detail=detail):
                error = ProcessTimeoutError(arguments, timeout_seconds)
                error.add_note(detail)
                raise error
            case ProcessOutputTooLarge(limit=limit, stream=stream, detail=detail):
                message = f"{' '.join(arguments)} wrote more than {limit} bytes to {stream}"
                error = OSError(message)
                if detail:
                    error.add_note(detail)
                raise error

    def spawn(self, argv: Sequence[str]) -> subprocess.Popen[str]:
        """Start *argv* in its own process group."""
        return start_process_group(argv, env=None, cwd=None)


def _text(output: bytes) -> str:
    """Decode captured output as ``subprocess.run(text=True)`` does, with universal newlines."""
    return output.decode().replace("\r\n", "\n").replace("\r", "\n")
