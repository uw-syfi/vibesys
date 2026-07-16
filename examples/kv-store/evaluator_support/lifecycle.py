"""Candidate service lifecycle owned by trusted KV-store evaluators."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from .net import free_port, wait_until_listening


@dataclass(frozen=True)
class CandidateTarget:
    """Resolved candidate endpoint for an evaluator invocation.

    ``process_group`` is set when the evaluator owns the launcher process group.
    External ``--port`` mode leaves it ``None`` so CPU accounting falls back to
    listener discovery.
    """

    port: int
    process_group: int | None = None
    pid: int | None = None


# Backwards-compatible alias used by older call sites / docs.
CandidateServer = CandidateTarget


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
) -> Iterator[CandidateTarget]:
    """Yield an external server or launch and clean up ``./run.sh``.

    When ``port`` is supplied, lifecycle ownership stays with the caller.
    Otherwise the evaluator starts the candidate in a new process group.
    """

    if port is not None:
        yield CandidateTarget(port=port)
        return

    selected_port = free_port()
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
    target = CandidateTarget(
        port=selected_port,
        pid=process.pid,
        process_group=process_group,
    )
    try:
        if not wait_until_listening(selected_port, startup_timeout):
            return_code = process.poll()
            detail = f" (launcher exited {return_code})" if return_code is not None else ""
            raise RuntimeError(
                f"candidate did not listen on port {selected_port} within "
                f"{startup_timeout:.1f}s{detail}"
            )
        yield target
    finally:
        _terminate_group(process, process_group)
