"""A command run to completion whose output is forwarded as it is written.

For a long build-like command (``docker build``) whose caller wants the
command's result exactly as :func:`subprocess.run` reports it, and also its
output while it runs, to follow or report progress from.
"""

from __future__ import annotations

import os
import subprocess
import threading
from typing import IO, TYPE_CHECKING, Protocol

from vs_sim.output import OutputStream

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from vs_sim.output import OutputSink

_READ_BYTES = 64 * 1024


class StreamingCommand(Protocol):
    """Runs one command to completion, writing its output to a sink as it arrives."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        output: OutputSink,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv`` with no input and no shell, and return what it printed.

        Every byte the command writes reaches ``output`` before this returns or
        raises; the result still holds all of it, decoded as UTF-8 with
        undecodable bytes replaced.

        Raises:
            FileNotFoundError: The program does not exist.
            subprocess.TimeoutExpired: The command outlived ``timeout_seconds``;
                it was killed.
        """
        ...


class SubprocessStreamingCommand:
    """Runs the command as a real operating-system process, blocking the calling thread.

    Stderr is drained on the calling thread and stdout on a helper thread, so the
    two streams reach ``output`` concurrently, as :class:`OutputSink` allows.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        output: OutputSink,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``argv``; undecodable bytes are replaced."""
        command = tuple(argv)
        process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-186101 [S603]; the caller supplies an argv vector it assembled itself, and no shell is involved.
            command,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout = bytearray()
        reader = threading.Thread(
            target=_drain,
            args=(process.stdout, OutputStream.STDOUT, output, stdout),
            daemon=True,
        )
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            process.kill()

        deadline = threading.Timer(timeout_seconds, expire)
        reader.start()
        deadline.start()
        stderr = bytearray()
        try:
            _drain(process.stderr, OutputStream.STDERR, output, stderr)
            process.wait()
            reader.join()
        finally:
            deadline.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
        out, err = _text(stdout), _text(stderr)
        if expired.is_set():
            raise subprocess.TimeoutExpired(command, timeout_seconds, out, err)
        return subprocess.CompletedProcess(command, process.returncode, out, err)


def _drain(
    pipe: IO[bytes] | None, stream: OutputStream, output: OutputSink, kept: bytearray
) -> None:
    """Read ``pipe`` to its end, forwarding each chunk to ``output`` and keeping it."""
    if pipe is None:
        return
    while chunk := os.read(pipe.fileno(), _READ_BYTES):
        output.write(stream, chunk)
        kept += chunk


def _text(data: bytearray) -> str:
    return data.decode("utf-8", errors="replace")
