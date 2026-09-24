"""Unconfined local shell sandbox for backends that run on the host.

Commands run through ``subprocess`` with ``shell=True`` as the VibeSys user, so
there is no isolation: this is the "no container" sandbox kind. Host confinement
of the agent CLI itself is a separate concern (:mod:`vs_sandbox.host_sandbox`).
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

from vs_sandbox.execution import SandboxExecutionResult

DEFAULT_EXECUTE_TIMEOUT = 120
DEFAULT_MAX_OUTPUT_CHARS = 100_000
_TIMEOUT_EXIT_CODE = 124


class LocalShellSandbox:
    """Run shell commands on the host, in ``root_dir``, with a controlled env."""

    def __init__(
        self,
        root_dir: str | Path,
        *,
        env: dict[str, str] | None = None,
        inherit_env: bool = False,
        timeout: int = DEFAULT_EXECUTE_TIMEOUT,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> None:
        """Configure the working directory, environment, and limits."""
        if timeout <= 0:
            raise ValueError(f"timeout must be positive, got {timeout}")
        self.root_dir = Path(root_dir).resolve()
        #: Mutable on purpose: device re-selection edits it between commands.
        self.env: dict[str, str] = dict(os.environ) if inherit_env else {}
        self.env.update(env or {})
        self._default_timeout = timeout
        self._max_output_chars = max_output_chars
        self._id = f"local-{uuid.uuid4().hex[:8]}"

    @property
    def id(self) -> str:
        """Return this sandbox's random identifier."""
        return self._id

    def execute(self, command: str, *, timeout: int | None = None) -> SandboxExecutionResult:
        """Run *command* and return combined output with stderr lines tagged.

        Stderr lines are prefixed ``[stderr]``, empty output becomes
        ``<no output>``, output past the cap is cut with a notice, and a
        non-zero exit appends ``Exit code: N``. A timeout yields exit code 124
        and any other launch failure exit code 1; neither raises.
        """
        if not command or not isinstance(command, str):
            return SandboxExecutionResult(
                output="Error: Command must be a non-empty string.", exit_code=1
            )
        effective_timeout = timeout if timeout is not None else self._default_timeout
        if effective_timeout <= 0:
            raise ValueError(f"timeout must be positive, got {effective_timeout}")
        try:
            proc = subprocess.run(  # noqa: S602  # lint-waiver: LW-007109 [S602]; this host sandbox boundary intentionally executes the requested shell command.
                command,
                check=False,
                shell=True,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env=self.env,
                cwd=str(self.root_dir),
            )
        except subprocess.TimeoutExpired:
            return SandboxExecutionResult(
                output=f"Error: Command timed out after {effective_timeout} seconds.",
                exit_code=_TIMEOUT_EXIT_CODE,
            )
        except Exception as exc:
            return SandboxExecutionResult(
                output=f"Error executing command ({type(exc).__name__}): {exc}", exit_code=1
            )

        parts = [proc.stdout] if proc.stdout else []
        if proc.stderr:
            parts.extend(f"[stderr] {line}" for line in proc.stderr.strip().split("\n"))
        output = "\n".join(parts) if parts else "<no output>"
        truncated = len(output) > self._max_output_chars
        if truncated:
            output = (
                output[: self._max_output_chars]
                + f"\n\n... Output truncated at {self._max_output_chars} characters."
            )
        if proc.returncode != 0:
            output = f"{output.rstrip()}\n\nExit code: {proc.returncode}"
        return SandboxExecutionResult(
            output=output,
            exit_code=proc.returncode,
            truncated=truncated,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
