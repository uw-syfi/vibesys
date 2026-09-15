"""Private local control channel for a foreground Kubernetes lifecycle."""

from __future__ import annotations

import json
import socket
import socketserver
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


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
            request = json.loads(self.rfile.readline())
            action = request.get("action") if isinstance(request, dict) else None
            callback = server.actions.get(action) if isinstance(action, str) else None
            if callback is None:
                response: dict[str, Any] = {
                    "ok": False,
                    "error": f"unknown Kubernetes lifecycle action: {action!r}",
                }
            else:
                callback()
                response = {"ok": True}
        except Exception as error:  # noqa: BLE001
            response = {"ok": False, "error": str(error)}
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


def request_action(path: Path, action: str) -> None:
    """Request one lifecycle action and wait for its completion."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path))
        client.sendall(json.dumps({"action": action}).encode() + b"\n")
        response_bytes = b""
        while not response_bytes.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            response_bytes += chunk
    response = json.loads(response_bytes)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Kubernetes lifecycle control failed"))
