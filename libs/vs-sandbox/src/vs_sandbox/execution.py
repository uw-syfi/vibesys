"""Bounded, stream-aware sandbox execution results."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

_TRUNCATION_MARKER = "\n...[truncated]...\n"


@dataclass
class SandboxExecutionResult:
    """A command's combined output, exit code, and each process stream."""

    output: str
    exit_code: int | None = None
    truncated: bool = False
    stdout: str = ""
    stderr: str = ""


class Sandbox(Protocol):
    """The command-execution contract every sandbox kind satisfies.

    Container lifetime (``start``/``stop``) is not part of it: only container
    sandboxes have one.
    """

    @property
    def id(self) -> str:
        """Return a stable identifier for this sandbox instance."""
        ...

    def execute(self, command: str, *, timeout: int | None = None) -> SandboxExecutionResult:
        """Run a shell command and return its bounded result."""
        ...


def bounded_execution_result(
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    max_output_chars: int,
) -> SandboxExecutionResult:
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
        return SandboxExecutionResult(
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

    return SandboxExecutionResult(
        output=bounded_stdout + bounded_stderr,
        exit_code=exit_code,
        truncated=True,
        stdout=bounded_stdout,
        stderr=bounded_stderr,
    )
