"""Unconfined local shell runner for backends that run on the host.

Commands run under ``/bin/sh -c`` as the VibeSys user, so
there is no isolation: this is the "no container" command runner. Host confinement
of the agent CLI itself is a separate concern (:mod:`vs_sandbox.host_sandbox`).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.command_execution import execute_command
from vs_sandbox.process_execution import PipedStoppableProcess
from vs_sandbox.step_marks import step_shell_argv

if TYPE_CHECKING:
    from vs_sandbox.execution import CommandResult
    from vs_sim.api import Event, OutputSink, PipedProcessLauncher, Threads

DEFAULT_EXECUTE_TIMEOUT = 120
DEFAULT_MAX_OUTPUT_CHARS = 100_000
# The shell ``subprocess`` uses for ``shell=True`` on POSIX.
_SHELL = "/bin/sh"


class LocalShellRunner:
    """Run shell commands on the host, in ``root_dir``, with a controlled env."""

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-261404 [PLR0913]; the working directory, two environment inputs, two limits, the clock and the process launcher are independent inputs of one runner.
        # > A settings object would group the injected clock with plain limits into a
        # > carrier only this constructor reads; folding inherit_env into env would make
        # > every caller copy os.environ itself.
        self,
        root_dir: str | Path,
        *,
        env: dict[str, str] | None = None,
        inherit_env: bool = False,
        timeout: int = DEFAULT_EXECUTE_TIMEOUT,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        threads: Threads,
        launcher: PipedProcessLauncher,
    ) -> None:
        """Configure the working directory, environment, and limits.

        *threads* is the clock a command's timeout is measured on and runs the
        workers that drain its output; *launcher* starts each command's shell.
        """
        if timeout <= 0:
            message = f"timeout must be positive, got {timeout}"
            raise ValueError(message)
        self.root_dir = Path(root_dir).resolve()
        #: Mutable on purpose: device re-selection edits it between commands.
        self.env: dict[str, str] = dict(os.environ) if inherit_env else {}
        self.env.update(env or {})
        self._default_timeout = timeout
        self._max_output_chars = max_output_chars
        self._threads = threads
        self._launcher = launcher
        self._id = f"local-{uuid.uuid4().hex[:8]}"

    @property
    def id(self) -> str:
        """Return this runner's random identifier."""
        return self._id

    def agent_path(self, host_path: Path | str) -> str:
        """Return the unchanged path seen by a host-local process."""
        return str(Path(host_path))

    def execute(
        self,
        command: str,
        *,
        timeout: int | None = None,
        cancel: Event | None = None,
        output: OutputSink,
    ) -> CommandResult:
        """Run *command* under ``/bin/sh -c`` and return its bounded result.

        The command runs in its own process group, so a timeout or a set
        *cancel* stops every process it started (``SIGTERM``, then ``SIGKILL``
        after a grace period). The result contract is documented in
        :mod:`vs_sandbox.command_execution`; every command runner shares it.
        """
        return execute_command(
            command,
            timeout=timeout,
            default_timeout=self._default_timeout,
            cancel=cancel,
            max_output_chars=self._max_output_chars,
            launch=lambda: PipedStoppableProcess(
                self._launcher.start(
                    step_shell_argv(_SHELL, command, self.env), cwd=self.root_dir, env=self.env
                ),
                threads=self._threads,
                output=output,
            ),
            threads=self._threads,
        )
