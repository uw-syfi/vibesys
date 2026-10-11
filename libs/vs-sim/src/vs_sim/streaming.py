"""A command run to completion whose diagnostic stream is read as it is written.

For a long build-like command (``docker build``) whose caller wants the
command's result exactly as :func:`subprocess.run` reports it, and also each
line of its standard error while it runs, to report progress from.
"""

from __future__ import annotations

import subprocess
import threading
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path


class StreamingCommand(Protocol):
    """Runs one command to completion, handing each line of its stderr to a callback."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        on_stderr_line: Callable[[str], None],
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` with no input and no shell, and return what it printed.

        ``on_stderr_line`` receives each line of standard error, without its
        line ending, in order, on the calling thread, before this returns; the
        result's ``stderr`` still holds all of it.

        Raises:
            FileNotFoundError: The program does not exist.
            subprocess.TimeoutExpired: The command outlived ``timeout_seconds``;
                it was killed.
        """
        ...


class SubprocessStreamingCommand:
    """Runs the command as a real operating-system process, blocking the calling thread."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        on_stderr_line: Callable[[str], None],
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv``; undecodable bytes are replaced."""
        command = tuple(argv)
        process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-186101 [S603]; the caller supplies an argv vector it assembled itself, and no shell is involved.
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        stdout: list[str] = []
        reader = threading.Thread(
            target=lambda: stdout.append(process.stdout.read() if process.stdout else ""),
            daemon=True,
        )
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            process.kill()

        deadline = threading.Timer(timeout_seconds, expire)
        reader.start()
        deadline.start()
        stderr: list[str] = []
        try:
            for line in process.stderr or ():
                stderr.append(line)
                on_stderr_line(line.rstrip("\r\n"))
            process.wait()
            reader.join()
        finally:
            deadline.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
        if expired.is_set():
            raise subprocess.TimeoutExpired(
                command, timeout_seconds, "".join(stdout), "".join(stderr)
            )
        return subprocess.CompletedProcess(
            command, process.returncode, "".join(stdout), "".join(stderr)
        )
