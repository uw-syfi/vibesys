"""The Docker CLI as a port, so a sandbox can run against a real or a fake daemon."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

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

    from vs_sim.api import PipedProcess, PipedProcessLauncher, Processes

# The stop script for one ``docker exec`` command. Passed as
# ``sh -c SCRIPT sh MARKER SIGNAL TENTHS SESSIONS SINCE`` so no operand needs
# shell quoting, and so a fake container can recognise the request by this
# exact script instead of parsing its text. It runs as the command's own user,
# who can read the command's ``/proc/<pid>/environ`` entries.
#
# A process belongs to the command when its environment carries the
# ``MARKER`` line or it shares a session with one that does. Docker starts
# every exec in a session of its own, so a descendant that clears its
# environment (``env -i``, ``Popen(env=...)``) is still found by its session.
# ``SESSIONS`` and ``SINCE`` carry what earlier stop requests learned (the
# command's session ids, and the earliest start time among its processes, in
# clock ticks), so a later request still finds a session whose marked
# processes have all exited.
#
# The script signals the command's processes with ``SIGNAL`` (empty: none),
# waits up to ``TENTHS`` tenths of a second for them to end, and prints one
# line: how many remain (or ``unknown``), then ``SINCE`` (``-`` when nothing
# was ever found), then the session ids. A count is ``unknown`` while some
# process outside those sessions, started no earlier than the command, has an
# environment the script cannot read (another user's, or a setuid program):
# it may be the command's, so its absence from the count is no evidence.
# Zombies and processes that vanish mid-scan are not counted.
#
# Residual, neither signalled nor counted: a readable descendant that clears
# its environment and either leaves the session (``setsid``) or outlives every
# marked process of its session before any stop request has seen that session.
SIGNAL_EXEC_SCRIPT = r"""m=$1; g=$2; S=" $4 "; T=$5
mine() {
  n=$((n+1)); case "$S" in *" $1 "*) ;; *) S="$S$1 ";; esac
  if [ -z "$T" ] || [ "$2" -lt "$T" ]; then T=$2; fi
  if [ -n "$g" ]; then kill -"$g" "$3" 2>/dev/null; fi
}
scan() {
  n=0; u=0
  for d in /proc/[0-9]*; do
    p=${d#/proc/}; [ "$p" = $$ ] && continue
    st=$(cat "$d/stat" 2>/dev/null) || continue
    set -- ${st##*) }; [ "$1" = Z ] && continue
    sid=$4; start=${20}
    case "$S" in *" $sid "*) mine "$sid" "$start" "$p"; continue;; esac
    if e=$(tr "\0" "\n" 2>/dev/null < "$d/environ"); then
      if printf "%s\n" "$e" | grep -qx -- "$m"; then mine "$sid" "$start" "$p"; fi
    elif [ -d "$d" ] && [ -n "$T" ] && [ "$start" -ge "$T" ]; then u=$((u+1)); fi
  done
  if [ "$u" -gt 0 ]; then r=unknown; else r=$n; fi
}
k=$g; g=; scan; scan; g=$k; scan; g=; i=0; scan
while [ "$r" != 0 ] && [ "$i" -lt "$3" ]; do sleep 0.1; i=$((i+1)); scan; done
echo "$r ${T:--}$S"
"""


@dataclass(frozen=True)
class ExecStopEvidence:
    """What stop requests have learned about one exec's processes.

    *sessions* are the session ids its processes were found in, and *since* is
    the earliest start time among them, in clock ticks since the container's
    host booted (``None`` until a process is found).
    """

    sessions: tuple[str, ...] = ()
    since: str | None = None

    def operands(self) -> tuple[str, str]:
        """Return the stop script's ``SESSIONS`` and ``SINCE`` operands."""
        return " ".join(self.sessions), self.since or ""


@dataclass(frozen=True)
class ExecStopReply:
    """One stop request's answer: the survivors (``None``: unknown) and the evidence."""

    survivors: int | None
    evidence: ExecStopEvidence


def parse_exec_stop_reply(stdout: str) -> ExecStopReply | None:
    """Parse the stop script's output line, or return ``None`` outside its protocol."""
    fields = stdout.split()
    if len(fields) < _STOP_REPLY_HEAD:
        return None
    count, since, *sessions = fields
    if not (count.isdigit() or count == _UNKNOWN):
        return None
    if not (since.isdigit() or since == "-") or not all(s.isdigit() for s in sessions):
        return None
    return ExecStopReply(
        survivors=int(count) if count.isdigit() else None,
        evidence=ExecStopEvidence(
            sessions=tuple(sessions), since=since if since.isdigit() else None
        ),
    )


_UNKNOWN = "unknown"
_STOP_REPLY_HEAD = 2

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

    def spawn(self, argv: Sequence[str]) -> PipedProcess:
        """Start *argv* as a process group leader with piped, text-mode output.

        Raises ``OSError`` when ``docker`` cannot be launched.
        """
        ...


class SubprocessDockerCli:
    """The real ``docker`` binary on ``PATH``."""

    def __init__(self, *, processes: Processes, launcher: PipedProcessLauncher) -> None:
        """Run synchronous Docker commands through *processes*, streamed ones through *launcher*."""
        self._processes = processes
        self._launcher = launcher

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

    def spawn(self, argv: Sequence[str]) -> PipedProcess:
        """Start *argv* in its own process group."""
        return self._launcher.start(tuple(argv))


def _text(output: bytes) -> str:
    """Decode captured output as ``subprocess.run(text=True)`` does, with universal newlines."""
    return output.decode().replace("\r\n", "\n").replace("\r", "\n")
