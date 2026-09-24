"""Private local control channel for a foreground Kubernetes lifecycle."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
_MAX_REQUEST_BYTES = 64 * 1024
_REQUEST_TIMEOUT_SECONDS = 0.5
_RESPONSE_TIMEOUT_SECONDS = 5.0
_CONNECT_TIMEOUT_SECONDS = 5.0
# Actions wrap kubectl waits that are individually bounded by the lifecycle timeout.
_ACTION_TIMEOUT_SECONDS = 3600.0


class KubernetesControlError(RuntimeError):
    """Report local lifecycle-control transport failures."""

    @classmethod
    def connection_failed(cls, error: OSError) -> KubernetesControlError:
        """Create an error for a failed local socket request."""
        return cls(f"Kubernetes lifecycle control failed: {error}")

    @classmethod
    def invalid_response(cls) -> KubernetesControlError:
        """Create an error for a malformed control response."""
        return cls("Kubernetes lifecycle control returned no valid response")


def _read_frame(connection: socket.socket, deadline: float) -> tuple[bytes | None, str | None]:
    """Read one newline-terminated request, bounded in size and total time."""
    buffer = b""
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None, "incomplete Kubernetes lifecycle request"
        connection.settimeout(remaining)
        try:
            chunk = connection.recv(4096)
        except OSError:
            return None, "incomplete Kubernetes lifecycle request"
        if not chunk:
            return None, "incomplete Kubernetes lifecycle request"
        buffer += chunk
        if len(buffer) > _MAX_REQUEST_BYTES and b"\n" not in buffer[:_MAX_REQUEST_BYTES]:
            return None, "Kubernetes lifecycle request is too large"
    frame = buffer[: buffer.index(b"\n") + 1]
    if len(frame) > _MAX_REQUEST_BYTES:
        return None, "Kubernetes lifecycle request is too large"
    return frame, None


class _ControlServer(socketserver.UnixStreamServer):
    allow_reuse_address = False

    def __init__(self, path: Path, actions: dict[str, Callable[[], None]]) -> None:
        self.actions = actions
        super().__init__(str(path), _ControlHandler)


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        server = self.server
        if not isinstance(server, _ControlServer):
            return
        try:
            deadline = time.monotonic() + _REQUEST_TIMEOUT_SECONDS
            frame, error = _read_frame(self.connection, deadline)
            if frame is None:
                response: dict[str, Any] = {"ok": False, "error": error or "invalid request"}
            else:
                request = json.loads(frame)
                action = request.get("action") if isinstance(request, dict) else None
                callback = server.actions.get(action) if isinstance(action, str) else None
                if callback is not None:
                    callback()
                    response = {"ok": True}
                else:
                    response = {
                        "ok": False,
                        "error": f"unknown Kubernetes lifecycle action: {action!r}",
                    }
        # lint-waiver: LW-008046 [BLE001]; callback failures must be serialized to the IPC caller regardless of their application exception type.
        except Exception as error:  # noqa: BLE001
            response = {"ok": False, "error": str(error)}
        self.connection.settimeout(_RESPONSE_TIMEOUT_SECONDS)
        with suppress(OSError):
            self.wfile.write(json.dumps(response).encode() + b"\n")


class LifecycleControlServer:
    """Serve serialized stop/start requests on a private Unix socket."""

    def __init__(self, path: Path, actions: dict[str, Callable[[], None]]) -> None:
        """Initialize a server that owns ``path`` for its lifetime."""
        self._path = path
        self._server = _ControlServer(path, actions)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> LifecycleControlServer:
        """Start serving lifecycle requests."""
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        """Stop serving and remove the socket."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
        self._path.unlink(missing_ok=True)


def request_action(
    path: Path, action: str, *, timeout_seconds: float = _ACTION_TIMEOUT_SECONDS
) -> None:
    """Request one lifecycle action and wait for its completion."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        try:
            client.settimeout(_CONNECT_TIMEOUT_SECONDS)
            client.connect(str(path))
            client.settimeout(timeout_seconds)
            client.sendall(json.dumps({"action": action}).encode() + b"\n")
            response_bytes = b""
            while not response_bytes.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                response_bytes += chunk
        except OSError as error:
            raise KubernetesControlError.connection_failed(error) from error
    try:
        response = json.loads(response_bytes)
    except json.JSONDecodeError as error:
        raise KubernetesControlError.invalid_response() from error
    if not isinstance(response, dict) or not response.get("ok"):
        message = response.get("error") if isinstance(response, dict) else None
        raise RuntimeError(message or "Kubernetes lifecycle control failed")
