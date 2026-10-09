"""Bounded, stream-aware command execution results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import threading

_TRUNCATION_MARKER = "\n...[truncated]...\n"


@dataclass
class CommandResult:
    """A command's combined output, exit code, and each process stream.

    ``cancelled`` is true when the caller's cancel event stopped the command.
    """

    output: str
    exit_code: int | None = None
    truncated: bool = False
    stdout: str = ""
    stderr: str = ""
    cancelled: bool = False


class CommandRunner(Protocol):
    """Execute one shell command and return its bounded result.

    A command runner isolates nothing. Confinement is a separate contract
    (:class:`~vs_sandbox.host_sandbox.WorkspaceSandbox`); a ``DockerSandbox``
    is both. Container lifetime (``start``/``stop``) is not part of this
    contract: only container runners have one.
    """

    @property
    def id(self) -> str:
        """Return a stable identifier for this runner instance."""
        ...

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: threading.Event | None = None,
    ) -> CommandResult:
        """Run a shell command and return its bounded result.

        Setting *cancel* while the command runs, or exceeding *timeout*,
        stops the command and every process it started (``SIGTERM``, then
        ``SIGKILL`` after a grace period). A cancel returns ``cancelled=True``;
        a timeout returns exit code 124. Either keeps the output written
        before the stop. ``output == stdout + stderr`` and never exceeds the
        sandbox's cap; a signalled command reports ``128 + N``; an empty
        command returns exit code 1 without running; a non-positive *timeout*
        raises ``ValueError``. A sandbox that cannot stop its commands must
        raise rather than ignore *cancel*. The full contract is in
        :mod:`vs_sandbox.command_execution` and ``tests/test_sandbox_contract.py``.
        """
        ...


def bounded_execution_result(
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    max_output_chars: int,
) -> CommandResult:
    """Return a compatible result whose combined output fits the character cap.

    Successful commands retain the original combined prefix. Failed commands
    reserve half of the available payload for the stderr tail, then give unused
    capacity back to stdout or stderr. This keeps the start of normal output and
    the end of failure diagnostics while preserving ``output == stdout + stderr``.
    The marker is attached to a stream whose content was truncated, so semantic
    stream events never label retained process output as the wrong stream.
    """
    limit = max(0, max_output_chars)
    combined = stdout + stderr
    if len(combined) <= limit:
        return CommandResult(
            output=combined,
            exit_code=exit_code,
            truncated=False,
            stdout=stdout,
            stderr=stderr,
        )

    marker = _TRUNCATION_MARKER[:limit]
    payload_budget = limit - len(marker)
    failed = exit_code not in (None, 0)
    if failed and stderr:
        stderr_chars = min(len(stderr), payload_budget // 2)
        stdout_chars = min(len(stdout), payload_budget - stderr_chars)
        stderr_chars = min(len(stderr), payload_budget - stdout_chars)
        bounded_stdout = stdout[:stdout_chars]
        bounded_stderr = stderr[-stderr_chars:] if stderr_chars else ""
        if stdout_chars < len(stdout):
            bounded_stdout += marker
        else:
            bounded_stderr = marker + bounded_stderr
    else:
        prefix = combined[:payload_budget]
        stdout_chars = min(len(stdout), len(prefix))
        bounded_stdout = prefix[:stdout_chars]
        bounded_stderr = prefix[stdout_chars:]
        if bounded_stderr:
            bounded_stderr += marker
        else:
            bounded_stdout += marker

    return CommandResult(
        output=bounded_stdout + bounded_stderr,
        exit_code=exit_code,
        truncated=True,
        stdout=bounded_stdout,
        stderr=bounded_stderr,
    )
