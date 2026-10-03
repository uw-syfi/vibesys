"""Start and stop the candidate's ``python3 server.py`` for the benchmark and checker.

The candidate contract (see ``OBJECTIVE.md``): ``server.py`` at the workspace
root, started as ``python3 server.py --model-path <dir> --host <h> --port <p>``,
serving ``GET /health`` (200 only when ready) and ``POST /v1/chat/completions``.
Engine sharding across the 4 GPUs is the candidate's business; this launcher
only starts one process group and tears it down.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 30000
# Weight loading speed is out of scope for the metric, so the budget is generous.
STARTUP_TIMEOUT_SECONDS = 5400.0
POLL_INTERVAL_SECONDS = 5.0
SHUTDOWN_GRACE_SECONDS = 30.0


def resolve_model_path(*, required: bool = True) -> str | None:
    """Return ``$MODEL_PATH``; raise with an actionable message when required and unset."""
    value = os.environ.get("MODEL_PATH")
    if value:
        return value
    if required:
        raise RuntimeError("MODEL_PATH is not set; pass --model-path or export MODEL_PATH.")
    return None


def build_launch_argv(*, model_path: str, host: str, port: int) -> list[str]:
    return [
        sys.executable,
        "server.py",
        "--model-path",
        model_path,
        "--host",
        host,
        "--port",
        str(port),
    ]


def start_server(
    *, workspace: Path, model_path: str, host: str, port: int, log_path: Path
) -> subprocess.Popen:
    """Launch server.py in its own process group with output going to ``log_path``."""
    if not (workspace / "server.py").is_file():
        raise RuntimeError(f"{workspace}/server.py not found; the candidate entrypoint is missing.")
    env = dict(os.environ, MODEL_PATH=model_path, PYTHONUNBUFFERED="1")
    log_file = log_path.open("w")
    return subprocess.Popen(
        build_launch_argv(model_path=model_path, host=host, port=port),
        cwd=str(workspace),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def stop_server(proc: subprocess.Popen) -> None:
    """Terminate the server's whole process group, escalating to SIGKILL."""
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.5)
    if proc.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)


def tail_server_log(log_path: Path, *, max_bytes: int = 4000) -> str:
    try:
        data = log_path.read_bytes()
    except OSError:
        return "(no log available)"
    return data[-max_bytes:].decode("utf-8", errors="replace")


async def wait_until_ready(
    base_url: str,
    *,
    proc: subprocess.Popen,
    timeout_seconds: float,
    log_path: Path,
) -> None:
    """Poll ``/health`` until it returns 200, the process dies, or we time out."""
    import aiohttp

    deadline = time.monotonic() + timeout_seconds
    last_error: str | None = None
    async with aiohttp.ClientSession() as session:
        while time.monotonic() < deadline:
            exit_code = proc.poll()
            if exit_code is not None:
                raise RuntimeError(
                    f"server.py exited early (code {exit_code}) before becoming ready; "
                    f"last log output:\n{tail_server_log(log_path)}"
                )
            try:
                async with session.get(
                    f"{base_url}/health", timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        return
                    last_error = f"/health returned status {resp.status}"
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
    raise RuntimeError(
        f"server.py did not become ready within {timeout_seconds:.0f}s "
        f"(last probe error: {last_error}); last log output:\n{tail_server_log(log_path)}"
    )


@contextlib.asynccontextmanager
async def server_endpoint(
    *,
    base_url: str | None,
    workspace: Path,
    model_path: str | None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    log_path: Path,
    startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
):
    """Yield a base URL: reuse ``base_url`` if given, else boot server.py and tear it down."""
    if base_url is not None:
        yield base_url
        return
    if model_path is None:
        raise RuntimeError("a model path is required to boot server.py")
    proc = start_server(
        workspace=workspace, model_path=model_path, host=host, port=port, log_path=log_path
    )
    booted_url = f"http://{host}:{port}"
    try:
        await wait_until_ready(
            booted_url, proc=proc, timeout_seconds=startup_timeout_seconds, log_path=log_path
        )
        yield booted_url
    finally:
        stop_server(proc)
