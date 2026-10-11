"""Bounded cleanup policy for one direct Slurm transport process."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_sim.api import (
    CapturedTextProcess,
    CapturedTextProcessLauncher,
    SubprocessCapturedTextProcessLauncher,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

_PROCESS_CLEANUP_TIMEOUT_SECONDS = 5.0


def _cleanup_failure_message(failure: BaseException) -> str:
    """Describe nested cleanup failures in their deterministic attempt order."""
    if isinstance(failure, BaseExceptionGroup):
        return "; ".join(_cleanup_failure_message(item) for item in failure.exceptions)
    return str(failure) or type(failure).__name__


@dataclass(frozen=True)
class OwnedProcessResult:
    """The unchanged argv, status, and UTF-8 streams of one completed command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class OwnedProcessRunner:
    """Run one transport command while owning its complete local process group."""

    def __init__(
        self,
        processes: CapturedTextProcessLauncher | None = None,
        *,
        cleanup_timeout: float = _PROCESS_CLEANUP_TIMEOUT_SECONDS,
    ) -> None:
        """Use the OS process adapter by default with a finite cleanup budget."""
        if not math.isfinite(cleanup_timeout) or cleanup_timeout < 0:
            message = "process cleanup timeout must be finite and nonnegative"
            raise ValueError(message)
        self._processes = (
            processes if processes is not None else SubprocessCapturedTextProcessLauncher()
        )
        self._cleanup_timeout = cleanup_timeout

    def run(self, argv: Sequence[str], *, stdin: str | None, timeout: float) -> OwnedProcessResult:
        """Run *argv*, stopping and settling its owned group after any failure."""
        command = tuple(argv)
        process = self._processes.start(command, pipe_stdin=stdin is not None)
        try:
            stdout, stderr = process.communicate(stdin, timeout)
        except BaseException as failure:
            self._settle(process, failure)
            raise
        try:
            return OwnedProcessResult(command, process.returncode, stdout, stderr)
        finally:
            process.close()

    def _settle(self, process: CapturedTextProcess, failure: BaseException) -> None:
        """Kill before reaping, then release every resource within one fixed wait."""
        try:
            # This must precede every poll or wait. The unreaped group leader
            # keeps its PID allocated, preventing its PGID from being reused
            # for an unrelated process group before the signal is delivered.
            process.kill_group()
        except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-195401 [BLE001]; process cleanup must preserve the transport timeout or interruption while still attempting the remaining cleanup steps.
            # > Restricting this to OS errors would let an injected process adapter
            # > replace the primary failure and skip bounded reaping.
            failure.add_note(f"process-group cleanup also failed: {cleanup_error}")
        try:
            process.wait(self._cleanup_timeout)
        except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-195402 [BLE001]; bounded reaping must preserve every primary failure, including interpreter-level cancellation.
            # > A narrower catch would allow an adapter failure to replace the
            # > caller-visible transport error and skip pipe closure.
            failure.add_note(f"process-group reap also failed: {cleanup_error}")
        try:
            process.close()
        except BaseException as cleanup_error:  # noqa: BLE001  # lint-waiver: LW-195403 [BLE001]; pipe closure is the last cleanup attempt and cannot replace the primary transport failure.
            # > Closing through a generic context manager would perform an
            # > unbounded wait; this adapter contract closes without waiting.
            detail = _cleanup_failure_message(cleanup_error)
            failure.add_note(f"process pipe cleanup also failed: {detail}")
