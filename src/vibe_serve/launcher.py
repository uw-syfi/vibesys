"""Launch one headless VibeServe backend and one terminal client."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from vibe_serve.constants import PROJECT_ROOT

_READY_TIMEOUT_SECONDS = 30.0
_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_BACKEND_EXIT_GRACE_SECONDS = 2.0


def launch(argv: list[str]) -> int:
    agent_cli = _selected_local_agent_cli(argv)
    if agent_cli is not None and shutil.which(agent_cli) is None:
        print(
            f"vibe-serve-launch: agent CLI {agent_cli!r} was not found on PATH.\n"
            f"  Install {agent_cli!r} and make it available in this shell, choose another\n"
            "  --cli-provider, or select --agent-backend deepagents.",
            file=sys.stderr,
        )
        return 1

    runtime = os.environ.get("VIBESERVE_TUI_RUNTIME") or shutil.which("bun")
    entrypoint = PROJECT_ROOT / "clients" / "tui" / "dist" / "index.js"
    if runtime is None:
        print("vibe-serve-launch: Bun is required by the OpenTUI client.", file=sys.stderr)
        return 1
    if not entrypoint.is_file():
        print("vibe-serve-launch: TUI build is missing; run `./vs ...`.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="vibeserve-session-") as session_dir:
        session = Path(session_dir)
        socket_path = session / "control.sock"
        backend_log_path = session / "backend.log"
        backend_command = [
            sys.executable,
            "-m",
            "vibe_serve.cli",
            *argv,
            "--headless",
            "--control-socket",
            str(socket_path),
        ]
        with backend_log_path.open("w", encoding="utf-8") as backend_log:
            backend = subprocess.Popen(
                backend_command,
                stdin=subprocess.DEVNULL,
                stdout=backend_log,
                stderr=backend_log,
                start_new_session=True,
            )
            frontend: subprocess.Popen[bytes] | None = None
            try:
                if not _wait_until_ready(socket_path, backend):
                    _report_backend_failure(backend, backend_log_path)
                    return backend.returncode or 1
                env = os.environ.copy()
                env["VIBESERVE_CONTROL_SOCKET"] = str(socket_path)
                frontend = subprocess.Popen([runtime, str(entrypoint)], env=env)
                return _monitor(frontend, backend, backend_log_path)
            except KeyboardInterrupt:
                return 130
            finally:
                if frontend is not None and frontend.poll() is None:
                    frontend.terminate()
                    _wait_or_kill(frontend)
                _terminate_backend(backend)


def _selected_local_agent_cli(argv: list[str]) -> str | None:
    """Return the required host CLI, or ``None`` when this run needs none."""
    from vibe_serve.cli import _PARSER_BUILDERS, _extract_loop_selection
    from vibe_serve.config import _load_config
    from vibe_serve.constants import DEFAULT_AGENT_BACKEND

    loop_kind, remaining = _extract_loop_selection(argv)
    args = _PARSER_BUILDERS[loop_kind]().parse_args(remaining)
    if getattr(args, "stub_agent", False) or args.modal or args.docker:
        return None
    config = _load_config(args.config)
    backend = args.agent_backend or config.agent.backend or DEFAULT_AGENT_BACKEND
    if backend != "cli":
        return None
    return args.cli_provider or config.agent.cli_provider or "codex"


def _wait_until_ready(socket_path: Path, backend: subprocess.Popen[bytes]) -> bool:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if backend.poll() is not None:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(0.5)
                connection.connect(str(socket_path))
                request = {
                    "protocol_version": 1,
                    "request_id": uuid.uuid4().hex,
                    "timestamp": "1970-01-01T00:00:00Z",
                    "type": "query.snapshot",
                }
                connection.sendall(json.dumps(request).encode() + b"\n")
                response = json.loads(_read_line(connection))
                if response.get("ok") is True:
                    return True
        except (FileNotFoundError, ConnectionError, TimeoutError, json.JSONDecodeError):
            time.sleep(0.05)
    return False


def _read_line(connection: socket.socket) -> str:
    chunks: list[bytes] = []
    while True:
        chunk = connection.recv(4096)
        if not chunk:
            raise ConnectionError("Backend closed before readiness response")
        chunks.append(chunk)
        joined = b"".join(chunks)
        if b"\n" in joined:
            return joined.partition(b"\n")[0].decode()


def _monitor(
    frontend: subprocess.Popen[bytes],
    backend: subprocess.Popen[bytes],
    backend_log_path: Path,
) -> int:
    while True:
        frontend_code = frontend.poll()
        backend_code = backend.poll()
        if frontend_code is not None:
            if backend_code is None:
                try:
                    backend_code = backend.wait(timeout=_BACKEND_EXIT_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    _terminate_backend(backend)
                    backend_code = 0
            if frontend_code != 0:
                return 130 if frontend_code in (-signal.SIGINT, 130) else frontend_code
            if backend_code not in (None, 0):
                _report_backend_failure(backend, backend_log_path)
            return backend_code if backend_code not in (None, 0) else 0
        if backend_code is not None:
            if backend_code != 0:
                _report_backend_failure(backend, backend_log_path)
            try:
                return frontend.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS) or backend_code
            except subprocess.TimeoutExpired:
                frontend.terminate()
                _wait_or_kill(frontend)
                return backend_code or 1
        time.sleep(0.05)


def _terminate_backend(backend: subprocess.Popen[bytes]) -> None:
    if backend.poll() is not None:
        return
    try:
        os.killpg(backend.pid, signal.SIGTERM)
        backend.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        os.killpg(backend.pid, signal.SIGKILL)
        backend.wait()


def _wait_or_kill(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(timeout=_SHUTDOWN_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _report_backend_failure(backend: subprocess.Popen[bytes], log_path: Path) -> None:
    backend.poll()
    try:
        tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
    except OSError:
        tail = []
    print(
        f"vibe-serve-launch: backend exited with status {backend.returncode or 1}",
        file=sys.stderr,
    )
    if tail:
        print("\n".join(tail), file=sys.stderr)


def main() -> None:
    raise SystemExit(launch(sys.argv[1:]))


if __name__ == "__main__":
    main()
