"""Local JSONL transport for presentation clients."""

from __future__ import annotations

import json
import os
import socketserver
import threading
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from vibe_serve.server.protocol import ProtocolRequest, Response
from vibe_serve.server.service import SupervisionService

_REQUEST_ADAPTER = TypeAdapter(ProtocolRequest)


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        service: SupervisionService = self.server.service  # type: ignore[attr-defined]
        for line in self.rfile:
            request_id = "unknown"
            try:
                raw = json.loads(line)
                request_id = str(raw.get("request_id", request_id))
                request = _REQUEST_ADAPTER.validate_python(raw)
                response = service.execute(request)
            except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as exc:
                response = Response(
                    request_id=request_id,
                    ok=False,
                    error=str(exc),
                )
            self.wfile.write(response.model_dump_json().encode() + b"\n")
            self.wfile.flush()


class _UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, path: Path, service: SupervisionService):
        self.service = service
        super().__init__(str(path), _RequestHandler)


class SupervisionSocketServer:
    """Own a private Unix socket serving one or more concurrent clients."""

    def __init__(self, path: Path, service: SupervisionService):
        self.path = path
        self.service = service
        self._server: _UnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self._server = _UnixServer(self.path, self.service)
        os.chmod(self.path, 0o600)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="vibeserve-supervision-server",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> SupervisionSocketServer:
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
