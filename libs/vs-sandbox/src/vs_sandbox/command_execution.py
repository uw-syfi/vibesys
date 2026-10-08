"""One definition of ``Sandbox.execute`` semantics, shared by every sandbox kind.

A sandbox kind supplies only how to *launch* the command (a ``sh -c`` on the
host, a ``docker exec`` into a container) and, when the command lives outside
the launched process's group, how to signal it. Validation, process-tree
stopping, timeout and cancellation results, partial-output retention, and the
output cap all live here, so two kinds cannot disagree about them.

The result contract, which ``tests/test_sandbox_contract.py`` holds every
implementation to:

* An empty or non-string command is rejected without running: exit code 1,
  the reason on ``stderr``. A non-positive timeout raises ``ValueError``.
* ``output == stdout + stderr`` always, and ``len(output)`` never exceeds the
  cap. Past the cap ``truncated`` is true and the cut stream carries the
  truncation marker (see :func:`~vs_sandbox.execution.bounded_execution_result`).
* A normal exit reports the command's status. A signalled command reports
  ``128 + N`` (shell convention), never a negative number.
* A timeout stops the command's whole process tree, keeps the output written
  before the stop, appends the timeout notice to ``stderr``, and reports exit
  code 124 (GNU ``timeout``'s status, which the benchmark failure
  classification reads as a stage timeout: the candidate exceeding its budget).
* A cancel does the same with ``cancelled=True`` and a cancellation notice on
  ``stderr``; the exit code is the status the stopped command ended with.
* A command that cannot be launched reports exit code 1 and the error on
  ``stderr``; no execution error raises.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vs_sandbox.execution import SandboxExecutionResult, bounded_execution_result
from vs_sandbox.process_execution import (
    DEFAULT_TERMINATION_GRACE_SECONDS,
    ProcessOutcome,
    ProcessStop,
    wait_stoppable,
)

if TYPE_CHECKING:
    import signal
    import threading
    from collections.abc import Callable

#: GNU ``timeout``'s exit status when it stops a command on its limit.
TIMEOUT_EXIT_CODE = 124
_INVALID_COMMAND_EXIT_CODE = 1
_LAUNCH_FAILURE_EXIT_CODE = 1


def validate_timeout(timeout: int | None, default: int) -> int:
    """Return the effective timeout, rejecting a non-positive one."""
    effective = timeout if timeout is not None else default
    if effective <= 0:
        message = f"timeout must be positive, got {effective}"
        raise ValueError(message)
    return effective


def rejected_result(message: str, exit_code: int, max_output_chars: int) -> SandboxExecutionResult:
    """Return a result for a command that never ran: *message* on ``stderr``."""
    return bounded_execution_result(
        stdout="", stderr=f"{message}\n", exit_code=exit_code, max_output_chars=max_output_chars
    )


def execute_command(  # noqa: PLR0913  # lint-waiver: LW-731010 [PLR0913]; the shared contract takes the Sandbox.execute arguments plus the two launch hooks, and bundling them would only rename the same keywords.
    command: str,
    *,
    timeout: int | None,
    default_timeout: int,
    cancel: threading.Event | None,
    max_output_chars: int,
    launch: Callable[[], subprocess.Popen[str]],
    signal_remote: Callable[[signal.Signals], None] | None = None,
    grace_seconds: float = DEFAULT_TERMINATION_GRACE_SECONDS,
) -> SandboxExecutionResult:
    """Run *command* through *launch* under the module's result contract.

    *launch* starts the command as a process group leader with piped output
    (:func:`~vs_sandbox.process_execution.start_process_group`). *signal_remote*
    signals the command's processes where they actually live when that is
    outside the launched group.
    """
    if not command or not isinstance(command, str):
        return rejected_result(
            "Error: Command must be a non-empty string.",
            _INVALID_COMMAND_EXIT_CODE,
            max_output_chars,
        )
    effective_timeout = validate_timeout(timeout, default_timeout)
    try:
        process = launch()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return rejected_result(
            f"Error executing command ({type(exc).__name__}): {exc}",
            _LAUNCH_FAILURE_EXIT_CODE,
            max_output_chars,
        )
    outcome = wait_stoppable(
        process,
        timeout=effective_timeout,
        cancel=cancel,
        grace_seconds=grace_seconds,
        signal_remote=signal_remote,
    )
    return result_of(outcome, effective_timeout, max_output_chars)


def result_of(
    outcome: ProcessOutcome, timeout: int, max_output_chars: int
) -> SandboxExecutionResult:
    """Map one process outcome to the contract result."""
    stderr = outcome.stderr
    exit_code = outcome.returncode
    if outcome.stopped is ProcessStop.TIMEOUT:
        stderr = _with_notice(stderr, f"Error: Command timed out after {timeout} seconds.")
        exit_code = TIMEOUT_EXIT_CODE
    elif outcome.stopped is ProcessStop.CANCELLED:
        stderr = _with_notice(stderr, "Error: Command was cancelled.")
    result = bounded_execution_result(
        stdout=outcome.stdout,
        stderr=stderr,
        exit_code=exit_code,
        max_output_chars=max_output_chars,
    )
    result.cancelled = outcome.stopped is ProcessStop.CANCELLED
    return result


def _with_notice(stream: str, notice: str) -> str:
    separator = "" if not stream or stream.endswith("\n") else "\n"
    return f"{stream}{separator}{notice}\n"
