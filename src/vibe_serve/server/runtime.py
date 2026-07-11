"""Compose a Python run server with the external TypeScript TUI client."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vibe_serve.constants import PROJECT_ROOT
from vibe_serve.server.registry import REGISTRY
from vibe_serve.server.service import SupervisionService
from vibe_serve.server.supervisor import RunSupervisor
from vibe_serve.server.transport import SupervisionSocketServer


class RunOutcome:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.value: Any = None
        self.error: BaseException | None = None

    def succeed(self, value: Any) -> None:
        with self._lock:
            self.value = value

    def fail(self, error: BaseException) -> None:
        with self._lock:
            self.error = error


class InteractiveClientError(RuntimeError):
    """The presentation client could not be started or exited unexpectedly."""


def _validate_client() -> None:
    """Fail before backend output is captured or the run is started."""
    runtime = os.environ.get("VIBESERVE_TUI_RUNTIME") or shutil.which("bun")
    entrypoint = PROJECT_ROOT / "clients" / "tui" / "dist" / "index.js"
    if runtime is None:
        raise InteractiveClientError(
            "cannot start the OpenTUI client because Bun was not found. "
            "Use `./vs ...` (recommended) to prepare and launch VibeServe. "
            "Alternatively, install Bun or use --headless."
        )
    if not entrypoint.is_file():
        raise InteractiveClientError(
            "cannot start the TUI because its compiled entrypoint is missing. "
            "Use `./vs ...` (recommended) to build and launch it automatically, "
            "or use --headless."
        )


def _start_client(socket_path: Path) -> subprocess.Popen[bytes]:
    """Start the client while it can still inherit the original TTY descriptors."""
    runtime = os.environ.get("VIBESERVE_TUI_RUNTIME") or shutil.which("bun")
    entrypoint = PROJECT_ROOT / "clients" / "tui" / "dist" / "index.js"
    assert runtime is not None
    assert entrypoint.is_file()
    env = os.environ.copy()
    env["VIBESERVE_CONTROL_SOCKET"] = str(socket_path)
    return subprocess.Popen([runtime, str(entrypoint)], env=env)


class _MessageCapture:
    """Convert Python and descendant-process output into protocol events."""

    def __init__(self, supervisor: RunSupervisor):
        self.supervisor = supervisor
        self._stdout = None
        self._stderr = None
        self._saved_stdout: int | None = None
        self._saved_stderr: int | None = None
        self._pipes: dict[str, tuple[int, int]] = {}
        self._streams: list[Any] = []
        self._readers: list[threading.Thread] = []

    def __enter__(self) -> _MessageCapture:
        sys.stdout.flush()
        sys.stderr.flush()
        self._stdout, self._stderr = sys.stdout, sys.stderr
        self._saved_stdout = os.dup(1)
        self._saved_stderr = os.dup(2)
        self._pipes = {"stdout": os.pipe(), "stderr": os.pipe()}
        os.dup2(self._pipes["stdout"][1], 1)
        os.dup2(self._pipes["stderr"][1], 2)
        stdout_stream = os.fdopen(os.dup(self._pipes["stdout"][1]), "w", buffering=1)
        stderr_stream = os.fdopen(os.dup(self._pipes["stderr"][1]), "w", buffering=1)
        self._streams = [stdout_stream, stderr_stream]
        sys.stdout, sys.stderr = stdout_stream, stderr_stream
        for stream, (read_fd, _) in self._pipes.items():
            reader = threading.Thread(
                target=self._read_pipe,
                args=(stream, read_fd),
                name=f"vibeserve-{stream}-messages",
                daemon=True,
            )
            reader.start()
            self._readers.append(reader)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        sys.stdout.flush()
        sys.stderr.flush()
        assert self._stdout is not None
        assert self._stderr is not None
        sys.stdout, sys.stderr = self._stdout, self._stderr
        for stream in self._streams:
            stream.close()
        assert self._saved_stdout is not None
        assert self._saved_stderr is not None
        os.dup2(self._saved_stdout, 1)
        os.dup2(self._saved_stderr, 2)
        for _, write_fd in self._pipes.values():
            os.close(write_fd)
        for reader in self._readers:
            reader.join()
        for read_fd, _ in self._pipes.values():
            os.close(read_fd)
        os.close(self._saved_stdout)
        os.close(self._saved_stderr)

    def _read_pipe(self, stream: str, read_fd: int) -> None:
        while chunk := os.read(read_fd, 64 * 1024):
            self.supervisor.publish_output(stream, chunk.decode("utf-8", errors="replace"))


def run_interactive(run: Callable[[], Any], *, exp_name: str) -> Any:
    """Serve supervision state while an OpenTUI client owns the terminal."""
    del exp_name
    _validate_client()
    supervisor = RunSupervisor()
    service = SupervisionService(supervisor)
    outcome = RunOutcome()
    REGISTRY.activate(supervisor)

    def target() -> None:
        try:
            value = run()
            outcome.succeed(value)
            supervisor.finish()
        except BaseException as exc:
            outcome.fail(exc)
            supervisor.finish(exc)

    worker = threading.Thread(target=target, name="vibeserve-run", daemon=False)
    try:
        with tempfile.TemporaryDirectory(prefix="vibeserve-tui-") as temp_dir:
            socket_path = Path(temp_dir) / "control.sock"
            with SupervisionSocketServer(socket_path, service):
                try:
                    client = _start_client(socket_path)
                except BaseException as exc:
                    raise InteractiveClientError(f"TUI client failed to launch: {exc}") from exc
                with _MessageCapture(supervisor):
                    worker.start()
                    client_code = client.wait()
                    if worker.is_alive():
                        # A detached presentation client must not leave a
                        # safe-point pause blocking the backend indefinitely.
                        supervisor.resume()
                    worker.join()
                if client_code != 0:
                    raise InteractiveClientError(
                        f"TUI client exited unexpectedly with status {client_code}"
                    )
    finally:
        REGISTRY.deactivate(supervisor)
    if outcome.error is not None:
        raise outcome.error
    return outcome.value
