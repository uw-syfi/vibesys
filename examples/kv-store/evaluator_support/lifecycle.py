"""Candidate service lifecycle owned by trusted KV-store evaluators."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CandidateServer:
    """One evaluator-launched candidate service."""

    port: int
    pid: int
    process_group: int


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_until_listening(port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return True
        except OSError:
            time.sleep(0.05)
    return False


def _terminate_group(process: subprocess.Popen[bytes], process_group: int) -> None:
    if process.poll() is not None:
        process.wait()
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process_group, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process_group, signal.SIGKILL)
        process.wait(timeout=5)


@contextlib.contextmanager
def candidate_server(
    *,
    workspace: Path,
    port: int | None = None,
    startup_timeout: float = 5.0,
) -> Iterator[CandidateServer | None]:
    """Yield an external server or launch and clean up ``./run.sh``.

    When ``port`` is supplied, lifecycle ownership stays with the caller and
    ``None`` is yielded. Otherwise the evaluator starts the candidate in a new
    process group and yields its identity for trusted CPU accounting.
    """

    if port is not None:
        yield None
        return

    selected_port = _free_port()
    launcher = workspace / "run.sh"
    if not launcher.is_file():
        raise FileNotFoundError(f"candidate launcher not found: {launcher}")

    process = subprocess.Popen(
        [str(launcher), str(selected_port)],
        cwd=workspace,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    process_group = os.getpgid(process.pid)
    server = CandidateServer(
        port=selected_port,
        pid=process.pid,
        process_group=process_group,
    )
    try:
        if not _wait_until_listening(selected_port, startup_timeout):
            return_code = process.poll()
            detail = f" (launcher exited {return_code})" if return_code is not None else ""
            raise RuntimeError(
                f"candidate did not listen on port {selected_port} within "
                f"{startup_timeout:.1f}s{detail}"
            )
        yield server
    finally:
        _terminate_group(process, process_group)
