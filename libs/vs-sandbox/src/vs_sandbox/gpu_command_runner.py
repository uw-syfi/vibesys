"""Trusted capture commands backed by one run's authorized Slurm GPU jobs."""

from __future__ import annotations

import codecs
from dataclasses import dataclass, field
from hashlib import sha256
from typing import TYPE_CHECKING, Never, Protocol, runtime_checkable

from vs_redaction.api import DiagnosticRedactor, redact_diagnostic_text
from vs_sandbox.agent_gpu_cap import CANCELLED_BEFORE_SUBMISSION
from vs_sandbox.command_execution import TIMEOUT_EXIT_CODE, rejected_result, validate_timeout
from vs_sandbox.execution import (
    CaptureExecutionIdentity,
    CaptureExecutionKind,
    CommandExecutionSettledError,
    CommandResult,
    CommandStopUnconfirmedError,
    DiagnosticStreams,
    bounded_execution_result,
)
from vs_sandbox.host_command_broker import GpuCommandSpec
from vs_sim.api import OutputStream

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_sandbox.agent_gpu import AgentGpuConfig, GpuJobOutcome
    from vs_sandbox.host_command_broker import AuthorizedGpuJob, GpuCommands
    from vs_sim.api import Event, OutputSink, Threads, Worker

_MAX_OUTPUT_CHARACTERS = 100_000
_STOP_POLL_SECONDS = 0.1


@dataclass(slots=True)
class _MergedOutput:
    """A bounded prefix and rolling tail of Slurm's merged byte stream."""

    limit: int
    total: int = 0
    prefix: bytearray = field(default_factory=bytearray)
    tail: bytearray = field(default_factory=bytearray)

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        kept = max(self.limit + 1, 1)
        room = max(kept - len(self.prefix), 0)
        self.prefix.extend(chunk[:room])
        self.tail.extend(chunk)
        if overflow := len(self.tail) - kept:
            del self.tail[:overflow]

    def parts(self, *, failed: bool) -> tuple[str, str]:
        """Return retention roles, never invented process-stream attribution."""
        prefix = bytes(self.prefix)
        if self.total <= len(prefix):
            merged = prefix.decode(errors="replace")
            return ("", merged) if failed else (merged, "")
        overlap = max(len(prefix) + len(self.tail) - self.total, 0)
        tail = bytes(self.tail[overlap:])
        return prefix.decode(errors="replace"), tail.decode(errors="replace")


@dataclass(slots=True)
class _DiagnosticOutput:
    """Redact the logical stream across byte chunks before bounded retention."""

    limit: int
    retained: _MergedOutput = field(init=False)
    decoder: codecs.IncrementalDecoder = field(init=False)
    redactor: DiagnosticRedactor = field(default_factory=DiagnosticRedactor)
    finished: bool = False

    def __post_init__(self) -> None:
        self.retained = _MergedOutput(self.limit)
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def append(self, chunk: bytes) -> None:
        """Consume raw bytes without retaining an unredacted diagnostic fragment."""
        safe = self.redactor.append(self.decoder.decode(chunk, final=False))
        if safe:
            self.retained.append(safe.encode())

    def parts(self, *, failed: bool) -> tuple[str, str]:
        """Finish the complete diagnostic stream and return its bounded roles."""
        if not self.finished:
            remainder = self.redactor.append(self.decoder.decode(b"", final=True))
            remainder += self.redactor.finish()
            if remainder:
                self.retained.append(remainder.encode())
            self.finished = True
        return self.retained.parts(failed=failed)


@dataclass(slots=True)
class _CombinedStop:
    """An external cancellation or the runner's exact-seconds deadline."""

    external: Event | None
    deadline: Event
    external_seen: Event
    threads: Threads

    def is_set(self) -> bool:
        if self.deadline.is_set():
            return True
        if self.external is not None and self.external.is_set():
            self.external_seen.set()
            return True
        return False

    def set(self) -> None:
        """Let a launcher request cancellation through the combined signal."""
        self.deadline.set()

    def clear(self) -> None:
        """Clear only the runner-owned signal; callers own the external signal."""
        self.deadline.clear()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait on the injected timeline until either signal or *timeout*."""
        if self.is_set():
            return True
        until = None if timeout is None else self.threads.now() + max(timeout, 0.0)
        while not self.is_set():
            if until is not None:
                remaining = until - self.threads.now()
                if remaining <= 0:
                    return self.is_set()
                self.threads.sleep(min(_STOP_POLL_SECONDS, remaining))
            else:
                self.threads.sleep(_STOP_POLL_SECONDS)
        return True


@dataclass(frozen=True, slots=True)
class _StopResultContext:
    """Signals and configured duration used to classify a settled job result."""

    deadline: Event
    external_seen: Event
    effective_timeout: int


class GpuCommandRunner:
    """Run trusted shell commands as confined Slurm GPU jobs.

    The Slurm allocation gets the caller's timeout rounded *up* to minutes, while
    an injected-thread deadline still cancels at the exact requested second. The
    scheduler limit therefore never silently replaces a tighter runtime bound.
    """

    def __init__(
        self,
        commands: GpuCommands,
        workspace: Path,
        *,
        threads: Threads,
        max_output_characters: int = _MAX_OUTPUT_CHARACTERS,
    ) -> None:
        """Bind the run policy, this session's sole workspace and injected threads."""
        self._commands = commands
        self._workspace = workspace.resolve()
        self._threads = threads
        self._max_output_characters = max_output_characters
        self._id = gpu_capture_execution_identity(commands.config, self._workspace).execution_id

    @property
    def id(self) -> str:
        """Return the stable run-prefixed identity of this runner."""
        return self._id

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: Event | None = None,
        output: OutputSink,
    ) -> CommandResult:
        """Run *command* in the session's GPU job and return bounded output."""
        if not command or not isinstance(command, str):
            return rejected_result(
                "Error: Command must be a non-empty string.", 1, self._max_output_characters
            )
        effective_timeout = validate_timeout(
            timeout, self._commands.config.default_time_minutes * 60
        )
        if cancel is not None and cancel.is_set():
            result = bounded_execution_result(
                stdout="",
                stderr="Error: Command was cancelled before submission.\n",
                exit_code=CANCELLED_BEFORE_SUBMISSION,
                max_output_chars=self._max_output_characters,
            )
            result.cancelled = True
            return result
        spec = GpuCommandSpec(
            workspace=self._workspace,
            cwd=self._workspace,
            argv=("/bin/sh", "-c", command),
            env={},
            gpus=1,
            time_minutes=(effective_timeout + 59) // 60,
            job_name=self._id,
        )
        try:
            job = self._commands.prepare(spec)
        except (OSError, ValueError) as exc:
            return self._rejected(exc)
        return self._run(job, effective_timeout, cancel, output)

    def reconcile(self, execution: CaptureExecutionIdentity) -> bool:
        """Cancel a durably recorded capture job and confirm scheduler absence."""
        return GpuCaptureRecovery(self._commands.config, self._commands.launcher).reconcile(
            self._workspace, execution
        )

    def _run(
        self,
        job: AuthorizedGpuJob,
        effective_timeout: int,
        cancel: Event | None,
        output: OutputSink,
    ) -> CommandResult:
        """Run one authorized job under the exact deadline and output bound."""
        finished = self._threads.event()
        deadline = self._threads.event()
        external_seen = self._threads.event()
        stop = _CombinedStop(cancel, deadline, external_seen, self._threads)

        def expire() -> None:
            if not finished.wait(effective_timeout):
                deadline.set()

        timer = self._threads.spawn(expire, name="vibesys-gpu-command-deadline")
        captured = _MergedOutput(self._max_output_characters)
        diagnostics = _DiagnosticOutput(self._max_output_characters)

        def write(chunk: bytes) -> None:
            # Slurm merges process streams before this boundary. Raw bytes stay
            # available to protocol consumers while diagnostics are redacted as
            # one logical stream before any prefix/tail retention.
            captured.append(chunk)
            diagnostics.append(chunk)
            output.write(OutputStream.STDOUT, chunk)

        outcome, execution_error = self._execute_job(job, write, stop)
        cleanup_errors = self._finish_deadline(finished, timer)
        if execution_error is not None:
            self._raise_execution_failure(captured, diagnostics, execution_error, cleanup_errors)
        if outcome is None:
            result = self._failed(
                captured,
                diagnostics,
                "Error executing command: no scheduler outcome\n",
            )
            raise CommandStopUnconfirmedError(result, survivors=None)
        if cleanup_errors:
            self._raise_cleanup_failure(captured, diagnostics, outcome, cleanup_errors)
        return self._result_from_outcome(
            captured,
            diagnostics,
            outcome,
            _StopResultContext(deadline, external_seen, effective_timeout),
        )

    @staticmethod
    def _execute_job(
        job: AuthorizedGpuJob,
        write: Callable[[bytes], None],
        stop: _CombinedStop,
    ) -> tuple[GpuJobOutcome | None, BaseException | None]:
        try:
            return job.run(write, stop), None
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-213412 [BLE001]; after the launcher may submit, every failure class must retain remote ownership unless settlement was explicitly returned.
            return None, error

    @staticmethod
    def _finish_deadline(finished: Event, timer: Worker) -> tuple[BaseException, ...]:
        """Attempt independent timer cleanup steps and retain every failure."""
        errors: list[BaseException] = []
        for label, cleanup in (("signal deadline", finished.set), ("join deadline", timer.join)):
            try:
                cleanup()
            except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-213413 [BLE001]; independent deadline cleanup steps are both attempted and cannot replace an unconfirmed remote-stop outcome.
                error.add_note(f"GPU command could not {label}")
                errors.append(error)
        return tuple(errors)

    def _raise_execution_failure(
        self,
        captured: _MergedOutput,
        diagnostics: _DiagnosticOutput,
        error: BaseException,
        cleanup_errors: tuple[BaseException, ...],
    ) -> Never:
        for cleanup_error in cleanup_errors:
            error.add_note(
                "GPU command deadline cleanup also failed: "
                f"{type(cleanup_error).__name__}: {cleanup_error}"
            )
        if isinstance(error, CommandStopUnconfirmedError):
            raise error
        result = self._failed(
            captured,
            diagnostics,
            f"Error executing command ({type(error).__name__}): {error}\n",
        )
        raise CommandStopUnconfirmedError(result, survivors=None) from error

    def _raise_cleanup_failure(
        self,
        captured: _MergedOutput,
        diagnostics: _DiagnosticOutput,
        outcome: GpuJobOutcome,
        errors: tuple[BaseException, ...],
    ) -> Never:
        error = errors[0]
        for extra in errors[1:]:
            error.add_note(
                f"additional GPU command cleanup failure: {type(extra).__name__}: {extra}"
            )
        if not outcome.termination_confirmed:
            result = self._failed(
                captured,
                diagnostics,
                f"Error executing command ({type(error).__name__}): {error}\n",
            )
            raise CommandStopUnconfirmedError(result, survivors=None) from error
        raise CommandExecutionSettledError(error) from error

    def _result_from_outcome(
        self,
        captured: _MergedOutput,
        diagnostics: _DiagnosticOutput,
        outcome: GpuJobOutcome,
        stop: _StopResultContext,
    ) -> CommandResult:
        status = outcome.exit_code
        if outcome.failure is not None:
            result = self._failed(
                captured,
                diagnostics,
                f"Error executing command ({type(outcome.failure).__name__}): {outcome.failure}\n",
            )
            if not outcome.termination_confirmed:
                raise CommandStopUnconfirmedError(result, survivors=None) from outcome.failure
            return result
        try:
            status, notice, cancelled = self._stop_result(status, stop)
        except BaseException as error:
            if outcome.termination_confirmed:
                raise CommandExecutionSettledError(error) from error
            result = self._failed(
                captured,
                diagnostics,
                f"Error executing command ({type(error).__name__}): {error}\n",
            )
            raise CommandStopUnconfirmedError(result, survivors=None) from error
        failed = status not in (None, 0)
        stdout, stderr = captured.parts(failed=failed)
        diagnostic_stdout, diagnostic_stderr = diagnostics.parts(failed=failed)
        stderr += notice
        diagnostic_stderr += redact_diagnostic_text(notice)
        result = bounded_execution_result(
            stdout=stdout,
            stderr=stderr,
            exit_code=status,
            max_output_chars=self._max_output_characters,
            diagnostics=DiagnosticStreams(diagnostic_stdout, diagnostic_stderr),
        )
        result.cancelled = cancelled
        if not outcome.termination_confirmed:
            raise CommandStopUnconfirmedError(result, survivors=None)
        return result

    @staticmethod
    def _stop_result(
        status: int,
        stop: _StopResultContext,
    ) -> tuple[int, str, bool]:
        if stop.deadline.is_set():
            return (
                TIMEOUT_EXIT_CODE,
                f"Error: Command timed out after {stop.effective_timeout} seconds.\n",
                False,
            )
        if stop.external_seen.is_set():
            return status, "Error: Command was cancelled.\n", True
        return status, "", False

    def _rejected(
        self,
        error: BaseException,
    ) -> CommandResult:
        """Return the bounded launch-failure shape shared by command runners."""
        stderr = f"Error executing command ({type(error).__name__}): {error}\n"
        return bounded_execution_result(
            stdout="",
            stderr=stderr,
            exit_code=1,
            max_output_chars=self._max_output_characters,
        )

    def _failed(
        self,
        captured: _MergedOutput,
        diagnostics: _DiagnosticOutput,
        notice: str,
    ) -> CommandResult:
        """Keep the merged prefix and rolling diagnostic tail plus *notice*."""
        stdout, stderr = captured.parts(failed=True)
        diagnostic_stdout, diagnostic_stderr = diagnostics.parts(failed=True)
        return bounded_execution_result(
            stdout=stdout,
            stderr=stderr + notice,
            exit_code=1,
            max_output_chars=self._max_output_characters,
            diagnostics=DiagnosticStreams(
                diagnostic_stdout,
                diagnostic_stderr + redact_diagnostic_text(notice),
            ),
        )


def gpu_capture_execution_identity(
    config: AgentGpuConfig, workspace: Path
) -> CaptureExecutionIdentity:
    """Return the exact durable scheduler identity for one run workspace."""
    workspace_tag = sha256(str(workspace.resolve()).encode()).hexdigest()[:8]
    return CaptureExecutionIdentity(
        kind=CaptureExecutionKind.SLURM_GPU,
        execution_id=f"{config.job_name_prefix}-capture-{workspace_tag}",
    )


@runtime_checkable
class _GpuJobReconciler(Protocol):
    def reconcile(self, job_name: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class GpuCaptureRecovery:
    """Reconcile only capture identities derived for this run and workspace.

    This owner deliberately needs neither an editor session nor a command runner.
    A restarted run can therefore settle retained candidate work before opening a
    new root workspace, while an identity copied from another workspace or run is
    never sent to the scheduler.
    """

    config: AgentGpuConfig
    launcher: object

    def reconcile(self, workspace: Path, execution: CaptureExecutionIdentity) -> bool:
        """Cancel *execution* only when its complete expected identity matches."""
        expected = gpu_capture_execution_identity(self.config, workspace)
        if execution != expected or not isinstance(self.launcher, _GpuJobReconciler):
            return False
        try:
            settled = self.launcher.reconcile(expected.execution_id)
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-213424 [BLE001]; restart recovery retains durable ownership after every scheduler-boundary failure, including host cancellation.
            return False
        return settled if isinstance(settled, bool) else False


__all__ = ["GpuCaptureRecovery", "GpuCommandRunner", "gpu_capture_execution_identity"]
