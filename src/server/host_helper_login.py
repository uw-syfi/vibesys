"""Starting a harness's own login on the helper's machine, and telling the person what to do.

``LoginStarter.start`` runs the login command the agent library names (agentshim's facts, the
choice of variant in ``vs_agent``) as a child that outlives the request: a device-code login
prints a page to open and a code to enter, then waits for the person to finish. The starter reads
the output until it has that instruction, answers, and leaves the child running; a worker waits
for the child so it is reaped, and ends it if the person never finishes.

Processes and time come through ``vs_sim``'s ``PipedProcessLauncher`` and ``Threads``, so a test
scripts the CLI's output and the clock. Light: ``vs_sim`` and ``server.host_agent_logins``.

Public surface: ``LoginBounds``, ``LoginStarter``, ``login_instruction``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from server.host_agent_logins import LoginStarted, LoginUnavailable, LoginUnavailableReason
from vs_sim.api import ProcessTimeoutError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from vs_sim.api import PipedProcess, PipedProcessLauncher, Threads

_ANSI: Final = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_URL: Final = re.compile(r"https?://[^\s\x1b\"'<>)]+")
_CODE: Final = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,8}\b")
_MAX_LINE: Final = 300


def login_instruction(url: str | None, code: str | None) -> str | None:
    """What the person is asked to do, in one line, or ``None`` when nothing was shown.

    A page without a code is just the page. An instruction that would not fit a wire line is
    dropped rather than cut: half a URL is worse than none.
    """
    if url is None:
        return None
    line = f"Open {url} and enter the code {code}" if code is not None else f"Open {url}"
    return line if len(line) <= _MAX_LINE else None


class _Output:
    """What a login's output has shown so far; the reader writes, ``start`` waits on it."""

    def __init__(self, threads: Threads) -> None:
        self.condition = threads.condition()
        self.url: str | None = None
        self.code: str | None = None
        self.first_line = ""
        self.ended = False
        self.lines = 0

    def take(self, line: str) -> None:
        text = _ANSI.sub("", line).strip()
        with self.condition:
            self.lines += 1
            if text and not self.first_line:
                self.first_line = text[:_MAX_LINE]
            if self.url is None and (found := _URL.search(text)) is not None:
                self.url = found.group(0)
            if self.code is None and (found := _CODE.search(text)) is not None:
                self.code = found.group(0)
            self.condition.notify_all()

    def end(self) -> None:
        with self.condition:
            self.ended = True
            self.condition.notify_all()


@dataclass(frozen=True, slots=True)
class LoginBounds:
    """How long a login is waited for, in seconds of ``Threads``' clock."""

    page_wait: float = 10.0
    """The wait for the first page the login shows."""
    code_wait: float = 1.0
    """The further wait for a code once a page is shown."""
    lifetime: float = 900.0
    """How long a login may stay open before it is ended."""


class LoginStarter:
    """Runs a login command and reports what it asked the person to do."""

    def __init__(
        self,
        processes: PipedProcessLauncher,
        threads: Threads,
        environment: Callable[[], Mapping[str, str]],
        bounds: LoginBounds,
    ) -> None:
        """Start commands through ``processes``, in the environment ``environment()`` gives them.

        The environment is read when a login starts, not here: it is the login shell's, which a
        helper that never starts a login has no reason to load. ``threads`` is the clock waited on.
        """
        self._processes = processes
        self._threads = threads
        self._environment = environment
        self._bounds = bounds

    def start(self, argv: tuple[str, ...]) -> LoginStarted | LoginUnavailable:
        """Run ``argv`` and answer once it has shown its page and code, ended, or run out of time."""
        try:
            process = self._processes.start(argv, env=self._environment(), merge_stderr=True)
        except OSError as error:
            return _failed(f"could not run {argv[0]}: {error.strerror or error}")
        output = _Output(self._threads)
        self._threads.spawn(lambda: _read(process, output), name="login-output")
        self._threads.spawn(lambda: _reap(process, self._bounds.lifetime), name="login-reaper")
        with output.condition:
            output.condition.wait_for(
                lambda: output.url is not None or output.ended, self._bounds.page_wait
            )
            if output.url is not None:
                output.condition.wait_for(
                    lambda: output.code is not None or output.ended, self._bounds.code_wait
                )
            instruction = login_instruction(output.url, output.code)
            if instruction is None and output.url is None and output.ended and output.first_line:
                return _failed(output.first_line)
        return LoginStarted(instruction=instruction)


def _failed(detail: str) -> LoginUnavailable:
    return LoginUnavailable(
        reason=LoginUnavailableReason.START_FAILED, detail=detail.splitlines()[0][:_MAX_LINE]
    )


def _read(process: PipedProcess, output: _Output) -> None:
    """Feed the child's output lines to ``output`` until its pipe closes."""
    try:
        while line := process.stdout.readline():
            output.take(line)
    finally:
        output.end()


def _reap(process: PipedProcess, lifetime: float) -> None:
    """Wait for the child so it is reaped, and end its group if it outlives ``lifetime``."""
    try:
        process.wait(lifetime)
    except ProcessTimeoutError:
        process.terminate_group()
        try:
            process.wait(5.0)
        except ProcessTimeoutError:
            process.kill_group()
            process.wait(5.0)
