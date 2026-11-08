"""The launch contract: how a job starts the candidate's server, waits for it and stops it.

The candidate is the checkout's `engine` package, started from the checkout's root as

    uv run --project <root> python -m engine.server --model-dir <dir> --host 127.0.0.1 --port <port>

in the environment of the root's `pyproject.toml`, which is the candidate's to change. The port is
free when the job picks it. The server must answer `GET /health` with 2xx once it can serve
`POST /v1/completions`, within `STARTUP_TIMEOUT_SECONDS`.

The server runs in a process group of its own. It is ended with the group, whatever way the job
ends: on a normal exit, on an error, on SIGTERM (the CLI's timeout or cancel), and when this
process is killed outright, because a shell guard in the group kills it when the pipe this process
holds closes.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx

STARTUP_TIMEOUT_SECONDS = 300.0
STOP_GRACE_SECONDS = 10.0
LOG_TAIL_LINES = 15

# The guard: a watcher in the server's group waits for the end of the file on descriptor `fd`, a
# pipe whose write end only the job holds, and kills the group when it comes. Then the shell becomes
# the server, which does not inherit the descriptor.
_GUARD = """( read -r _ <&{fd}; kill -s KILL -- "-$$" ) &
exec "$@" {fd}<&-"""


class ServerError(Exception):
    """The candidate's server did not start or ended early; the message says why."""


def command(root: Path, model_dir: Path, port: int) -> list[str]:
    """The argv that starts the candidate's server."""
    return [
        "uv", "run", "--project", str(root),
        "python", "-m", "engine.server",
        "--model-dir", str(model_dir),
        "--host", "127.0.0.1",
        "--port", str(port),
    ]  # fmt: skip


def free_port() -> int:
    """A TCP port nothing listens on at this moment."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@contextlib.contextmanager
def serving(root: Path, model_dir: Path, log: Path) -> Iterator[str]:
    """Start the candidate's server, yield its base URL once `/health` answers, stop it on exit.

    Raises:
        ServerError: The server exited, or did not answer `/health`, within the startup deadline.
    """
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    # `uv run` exports its own environment's name; the candidate's project has a name of its own.
    env = {name: value for name, value in os.environ.items() if name != "VIRTUAL_ENV"}
    lifeline_read, lifeline_write = os.pipe()
    try:
        with log.open("wb") as sink:
            server = subprocess.Popen(
                [
                    "/bin/sh",
                    "-c",
                    _GUARD.format(fd=lifeline_read),
                    "vs-serve",
                    *command(root, model_dir, port),
                ],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.STDOUT,
                pass_fds=(lifeline_read,),
                start_new_session=True,
            )
        os.close(lifeline_read)
        try:
            _wait_ready(server, url, log)
            yield url
        finally:
            _stop(server)
    finally:
        os.close(lifeline_write)


def _wait_ready(server: subprocess.Popen[bytes], url: str, log: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        code = server.poll()
        if code is not None:
            raise ServerError(
                f"the server exited with {code} before /health answered\n{_tail(log)}"
            )
        try:
            if httpx.get(f"{url}/health", timeout=2.0).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise ServerError(
        f"/health did not answer 2xx within {STARTUP_TIMEOUT_SECONDS:.0f} s\n{_tail(log)}"
    )


def _stop(server: subprocess.Popen[bytes]) -> None:
    """End the server's whole group: SIGTERM, a grace period for the leader, then SIGKILL."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(server.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        server.wait(timeout=STOP_GRACE_SECONDS)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(server.pid, signal.SIGKILL)
    server.wait()


def _tail(log: Path) -> str:
    lines = log.read_text(errors="replace").splitlines()[-LOG_TAIL_LINES:]
    return "server log, last lines:\n" + "\n".join(lines)
