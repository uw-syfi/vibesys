"""Child processes behind an interface, so a test can script one on a virtual clock."""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    import signal
    from collections.abc import Mapping, Sequence
    from pathlib import Path


@dataclass(frozen=True)
class ProcessSpec:
    """What to run: the argument vector, its working directory and environment, its input."""

    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] | None = None
    input: bytes = b""

    def __post_init__(self) -> None:
        """Reject an empty command."""
        if not self.argv:
            message = "a process needs a program: argv is empty"
            raise ValueError(message)


@dataclass(frozen=True)
class ProcessOutcome:
    """How a process ended. A signal death has a negative ``returncode``, as in ``subprocess``."""

    returncode: int
    stdout: bytes
    stderr: bytes


class RunningProcess(Protocol):
    """A started process."""

    async def wait(self) -> ProcessOutcome:
        """Wait for it to end, feeding its input and collecting its output."""
        ...

    def terminate(self) -> None:
        """Ask it to stop (SIGTERM); a no-op once it has ended."""
        ...

    def kill(self) -> None:
        """Make it stop (SIGKILL); a no-op once it has ended."""
        ...


class ProcessLauncher(Protocol):
    """Starts child processes."""

    async def start(self, spec: ProcessSpec) -> RunningProcess:
        """Start ``spec``; raises ``OSError`` when the program cannot be started."""
        ...


class _SubprocessHandle:
    def __init__(self, process: asyncio.subprocess.Process, data: bytes) -> None:
        self._process = process
        self._input = data
        self._outcome: ProcessOutcome | None = None

    async def wait(self) -> ProcessOutcome:
        if self._outcome is None:
            stdout, stderr = await self._process.communicate(self._input)
            returncode = await self._process.wait()
            self._outcome = ProcessOutcome(returncode, stdout, stderr)
        return self._outcome

    def terminate(self) -> None:
        if self._process.returncode is None:
            self._process.terminate()

    def kill(self) -> None:
        if self._process.returncode is None:
            self._process.kill()


class SubprocessLauncher:
    """Starts real operating-system processes on the running event loop."""

    async def start(self, spec: ProcessSpec) -> RunningProcess:
        """Start ``spec`` with piped input and output."""
        process = await asyncio.create_subprocess_exec(
            *spec.argv,
            cwd=spec.cwd,
            env=None if spec.env is None else dict(spec.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        return _SubprocessHandle(process, spec.input)


class ForegroundChild(Protocol):
    """A started child that shares this process's terminal."""

    async def wait(self) -> int:
        """Wait for it to end; its exit status, negative when a signal ended it."""
        ...

    def send_signal(self, number: signal.Signals) -> None:
        """Deliver ``number`` to it; a no-op once it has ended."""
        ...


class ForegroundLauncher(Protocol):
    """Starts children that inherit stdin, stdout and stderr, as a shell's foreground job does."""

    async def start(
        self, argv: Sequence[str], env: Mapping[str, str] | None = None
    ) -> ForegroundChild:
        """Start ``argv`` (the environment of this process when ``env`` is ``None``).

        Raises ``OSError`` when the program cannot be started.
        """
        ...


class _InheritedHandle:
    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    async def wait(self) -> int:
        return await self._process.wait()

    def send_signal(self, number: signal.Signals) -> None:
        if self._process.returncode is None:
            self._process.send_signal(number)


class InheritedStdioLauncher:
    """Starts real operating-system processes that use this process's terminal."""

    async def start(
        self, argv: Sequence[str], env: Mapping[str, str] | None = None
    ) -> ForegroundChild:
        """Start ``argv`` on the running event loop with inherited standard streams."""
        process = await asyncio.create_subprocess_exec(
            *argv, env=None if env is None else dict(env)
        )
        return _InheritedHandle(process)
