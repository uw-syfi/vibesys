"""In-memory :class:`~vs_sandbox.execution.CommandRunner` test double.

Mirrors the observable contract of the real runners (an ``id`` property, an
``execute`` method returning :class:`~vs_sandbox.execution.CommandResult`)
without a subprocess: no shell is ever spawned. A caller scripts specific
commands with :meth:`FakeCommandRunner.script` (a canned result), or with
:meth:`FakeCommandRunner.script_process` and :meth:`FakeCommandRunner.script_hang`, which
describe what the command's process did and let the fake build the result
through the same mapping the real runners use
(:func:`~vs_sandbox.command_execution.result_of`), so truncation, timeout and
cancellation results cannot drift from production. Anything unscripted falls
back to a configurable default result (a clean success by default), and every
call is recorded for direct assertions.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.command_execution import rejected_result, result_of, validate_timeout
from vs_sandbox.execution import CommandResult
from vs_sandbox.process_execution import ProcessOutcome, ProcessStop

if TYPE_CHECKING:
    import threading

#: Result an unscripted command receives when no other default was set.
DEFAULT_RESULT = CommandResult(output="", exit_code=0, stdout="", stderr="")

_DEFAULT_TIMEOUT_SECONDS = 120
_DEFAULT_MAX_OUTPUT_CHARS = 100_000
#: Status of a command stopped by ``SIGTERM`` (128 + 15).
_SIGTERM_STATUS = 143


@dataclass(frozen=True, slots=True)
class FakeExecution:
    """One recorded call to :meth:`FakeCommandRunner.execute`."""

    command: str
    timeout: int | None
    cancellable: bool = False


@dataclass(frozen=True, slots=True)
class _Hang:
    """A command that wrote *stdout*/*stderr* and then blocks until stopped."""

    stdout: str
    stderr: str


@dataclass(slots=True)
class FakeCommandRunner:
    """Configurable in-memory double for :class:`~vs_sandbox.execution.CommandRunner`."""

    _id: str = field(default_factory=lambda: f"fake-{uuid.uuid4().hex[:8]}")
    default_result: CommandResult = field(default_factory=lambda: DEFAULT_RESULT)
    max_output_chars: int = _DEFAULT_MAX_OUTPUT_CHARS
    calls: list[FakeExecution] = field(default_factory=list)
    _scripted: dict[str, CommandResult | ProcessOutcome | _Hang] = field(default_factory=dict)

    @property
    def id(self) -> str:
        """Return this runner's identifier."""
        return self._id

    def agent_path(self, host_path: Path | str) -> str:
        """Return the unchanged path seen by a host-local agent."""
        return str(Path(host_path))

    def script(self, command: str, result: CommandResult) -> None:
        """Return *result* the next time (and every time) *command* is executed."""
        self._scripted[command] = result

    def script_process(
        self, command: str, *, stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> None:
        """Make *command* write *stdout*/*stderr* and exit with *returncode*.

        The result goes through the same mapping as a real process's, so it is
        bounded by :attr:`max_output_chars` and truncation is reported.
        """
        self._scripted[command] = ProcessOutcome(stdout, stderr, returncode)

    def script_hang(self, command: str, *, stdout: str = "", stderr: str = "") -> None:
        """Make *command* write *stdout*/*stderr* and then run until stopped.

        A call with a *cancel* event returns when the event is set, as a
        cancelled result; a call without one is stopped by its timeout at
        once (the fake has no clock), as a timeout result.
        """
        self._scripted[command] = _Hang(stdout, stderr)

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: threading.Event | None = None,
    ) -> CommandResult:
        """Return the scripted result for *command*, or the default result.

        Applies the same validation as the real runners: an empty or
        non-string command never reaches the script table, and a non-positive
        timeout raises ``ValueError``. A fake command finishes instantly, so
        *cancel* is honored when it is already set.
        """
        if not command or not isinstance(command, str):
            return rejected_result(
                "Error: Command must be a non-empty string.", 1, self.max_output_chars
            )
        effective_timeout = validate_timeout(timeout, _DEFAULT_TIMEOUT_SECONDS)
        self.calls.append(
            FakeExecution(command=command, timeout=timeout, cancellable=cancel is not None)
        )
        scripted = self._scripted.get(command, self.default_result)
        if cancel is not None and cancel.is_set():
            outcome = ProcessOutcome("", "", _SIGTERM_STATUS, ProcessStop.CANCELLED)
        elif isinstance(scripted, _Hang):
            stop = ProcessStop.TIMEOUT
            if cancel is not None:
                cancel.wait()
                stop = ProcessStop.CANCELLED
            outcome = ProcessOutcome(scripted.stdout, scripted.stderr, _SIGTERM_STATUS, stop)
        elif isinstance(scripted, ProcessOutcome):
            outcome = scripted
        else:
            return scripted
        return result_of(outcome, effective_timeout, self.max_output_chars)


@dataclass(slots=True)
class FakeLifecycleRunner(FakeCommandRunner):
    """In-memory command runner with an explicit start/stop lifecycle."""

    start_count: int = 0
    stop_count: int = 0
    start_error: Exception | None = None
    stop_error: Exception | None = None

    def start(self) -> None:
        """Record one lifecycle start attempt and raise a scripted error."""
        self.start_count += 1
        if self.start_error is not None:
            raise self.start_error

    def stop(self) -> None:
        """Record one lifecycle stop attempt and raise a scripted error."""
        self.stop_count += 1
        if self.stop_error is not None:
            raise self.stop_error
