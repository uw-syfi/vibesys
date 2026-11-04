"""Bounded, stream-aware command execution results."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from vs_redaction.api import redact_diagnostic_text

if TYPE_CHECKING:
    from vs_sim.api import Event, OutputSink

_TRUNCATION_MARKER = "\n...[truncated]...\n"
_MISSING_DIAGNOSTIC = cast("str", object())


class CaptureExecutionKind(StrEnum):
    """Closed kinds of external execution a captured command can leave behind."""

    SLURM_GPU = "slurm_gpu"


class CaptureExecutionIdentity(BaseModel):
    """Validated durable identity used to reconcile one captured command.

    The identifier is deliberately restricted to the scheduler/container-safe
    alphabet used by the implementations.  Durable state therefore cannot turn
    an arbitrary string into a controller query after restart.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: CaptureExecutionKind
    execution_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    )


@dataclass
class CommandResult:
    """A command's combined output, exit code, and each process stream.

    ``output``/``stdout``/``stderr`` preserve command bytes for protocols and
    evidence. Their ``diagnostic_*`` counterparts redact recognized credentials
    from the complete streams before applying the same runner output cap, so
    operator-facing consumers never need to sanitize an already-truncated key.
    ``cancelled`` is true when the caller's cancel event stopped the command.
    """

    output: str
    exit_code: int | None = None
    truncated: bool = False
    stdout: str = ""
    stderr: str = ""
    cancelled: bool = False
    diagnostic_output: str = _MISSING_DIAGNOSTIC
    diagnostic_stdout: str = _MISSING_DIAGNOSTIC
    diagnostic_stderr: str = _MISSING_DIAGNOSTIC

    def __post_init__(self) -> None:
        """Supply a safe diagnostic view for manually constructed results.

        Production runners provide this view from complete process streams before
        bounding them. Directly constructed results, used by adapters and Fakes,
        have no earlier stream available, so their retained text is redacted here.
        """
        self.diagnostic_output = self._safe_view(
            "diagnostic_output", self.output, self.diagnostic_output
        )
        self.diagnostic_stdout = self._safe_view(
            "diagnostic_stdout", self.stdout, self.diagnostic_stdout
        )
        self.diagnostic_stderr = self._safe_view(
            "diagnostic_stderr", self.stderr, self.diagnostic_stderr
        )

    def _safe_view(self, name: str, raw: str, safe: str) -> str:
        """Derive only from a complete stream; require callers to preserve earlier context."""
        if safe is not _MISSING_DIAGNOSTIC:
            return safe
        if not raw:
            return ""
        if self.truncated:
            message = f"a truncated command result requires {name} from before truncation"
            raise ValueError(message)
        return redact_diagnostic_text(raw)


class CommandExecutionSettledError(RuntimeError):
    """A local command-runner failure after external termination was confirmed.

    This typed proof lets lifecycle owners surface the local failure while still
    clearing durable capture intent. It must only be constructed at the boundary
    that observed a settled external outcome.
    """

    def __init__(self, cause: BaseException) -> None:
        """Retain the local cleanup failure without weakening its traceback."""
        self.cause = cause
        super().__init__(f"settled command cleanup failed: {type(cause).__name__}: {cause}")


@dataclass(frozen=True, slots=True)
class DiagnosticStreams:
    """Credential-safe streams whose redaction preceded every retention bound.

    A streaming runner may already have retained a safe prefix/tail; the
    important proof is that no earlier cap could split a credential before it
    was recognized and redacted.
    """

    stdout: str
    stderr: str


class CommandStopUnconfirmedError(RuntimeError):
    """A stopped command's processes could not be shown to have ended where they run.

    Killing a local client does not necessarily stop the command where it runs.
    When a container process check finds survivors, a Slurm queue check still
    lists the named job, or the check cannot be made, the command may still be
    running and writing; ``result`` is what the stop would otherwise have returned.
    """

    def __init__(self, result: CommandResult, survivors: int | None) -> None:
        """Record the unconfirmed *result* and how many remote processes remain, if known."""
        self.result = result
        self.survivors = survivors
        found = "could not be checked" if survivors is None else f"left {survivors} running"
        super().__init__(f"stopping the command {found} where it runs")


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
        cancel: Event | None = None,
        output: OutputSink,
    ) -> CommandResult:
        """Run a shell command and return its bounded result.

        Every chunk the command writes to stdout or stderr is also written to
        *output* as it is read, in order per stream, past the cap too and before
        a timeout or cancel stops it; nothing is written after the call returns.
        The result is the same whatever *output* is.

        Setting *cancel* while the command runs, or exceeding *timeout*,
        stops the command and every process it started (``SIGTERM``, then
        ``SIGKILL`` after a grace period). A cancel returns ``cancelled=True``;
        a timeout returns exit code 124. Either keeps the output written
        before the stop. ``output == stdout + stderr`` and never exceeds the
        sandbox's cap; a signalled command reports ``128 + N``; an empty
        command returns exit code 1 without running; a non-positive *timeout*
        raises ``ValueError``. A sandbox that cannot stop its commands must
        raise rather than ignore *cancel*. Raw result streams remain suitable
        for machine protocols; ``diagnostic_*`` streams are their bounded,
        credential-redacted operator-facing view. The full contract is in
        :mod:`vs_sandbox.command_execution` and ``tests/test_sandbox_contract.py``.
        """
        ...


class CaptureCommandRunner(Protocol):
    """Execute a trusted capture command through its attribution environment.

    This is deliberately narrower than :class:`CommandRunner`: Slurm exposes
    one merged diagnostic stream, so it cannot promise reconstructed stdout
    and stderr.  Every raw byte still reaches *output* as ``STDOUT``.  A
    successful result retains the bounded merged stream in ``stdout``; a
    failed, timed-out, or cancelled result retains it in ``stderr`` so the
    shared failure-tail policy keeps the newest diagnostic.  Runner-generated
    stop notices are returned but are not written to *output*.

    A preset or later *cancel* stops the command before returning.  If the
    remote allocation cannot be proved gone, the call raises
    :class:`CommandStopUnconfirmedError` with the otherwise returned result.
    A local cleanup fault after proven external settlement raises
    :class:`CommandExecutionSettledError` without weakening that proof.
    Ordinary :class:`CommandRunner` implementations also satisfy this role;
    they may preserve their stronger native stream attribution.
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
        cancel: Event | None = None,
        output: OutputSink,
    ) -> CommandResult:
        """Run one capture command and return its bounded diagnostics."""
        ...


def bounded_execution_result(
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    max_output_chars: int,
    diagnostics: DiagnosticStreams | None = None,
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
    bounded_stdout, bounded_stderr, truncated = _bounded_streams(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        limit=limit,
    )
    safe_stdout = redact_diagnostic_text(stdout) if diagnostics is None else diagnostics.stdout
    safe_stderr = redact_diagnostic_text(stderr) if diagnostics is None else diagnostics.stderr
    bounded_diagnostic_stdout, bounded_diagnostic_stderr, _ = _bounded_streams(
        stdout=safe_stdout,
        stderr=safe_stderr,
        exit_code=exit_code,
        limit=limit,
    )
    return CommandResult(
        output=bounded_stdout + bounded_stderr,
        exit_code=exit_code,
        truncated=truncated,
        stdout=bounded_stdout,
        stderr=bounded_stderr,
        diagnostic_output=bounded_diagnostic_stdout + bounded_diagnostic_stderr,
        diagnostic_stdout=bounded_diagnostic_stdout,
        diagnostic_stderr=bounded_diagnostic_stderr,
    )


def _bounded_streams(
    *,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    limit: int,
) -> tuple[str, str, bool]:
    """Bound one pair of streams without changing their contents first."""
    combined = stdout + stderr
    if len(combined) <= limit:
        return stdout, stderr, False

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

    return bounded_stdout, bounded_stderr, True
