"""Local JSONL transport for presentation clients."""

from __future__ import annotations

import json
import os
import socketserver
import threading
import time
from pathlib import Path

from pydantic import BaseModel, TypeAdapter, ValidationError

from vibe_serve.server.protocol import (
    EventBatchMessage,
    EventMessage,
    ProtocolRequest,
    Response,
    SubscribedMessage,
    SubscribeRequest,
)
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
                if isinstance(request, SubscribeRequest):
                    self.server.client_subscribed.set()  # type: ignore[attr-defined]
                    try:
                        self._stream(request)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    finally:
                        self.server.client_disconnected.set()  # type: ignore[attr-defined]
                    return
                response = service.execute(request)
            except (json.JSONDecodeError, TypeError, ValidationError, ValueError) as exc:
                response = Response(
                    request_id=request_id,
                    ok=False,
                    error=str(exc),
                )
            self.wfile.write(response.model_dump_json().encode() + b"\n")
            self.wfile.flush()

    def _stream(self, request: SubscribeRequest) -> None:
        service: SupervisionService = self.server.service  # type: ignore[attr-defined]
        snapshot = service.snapshot()
        self._write_message(
            SubscribedMessage(
                request_id=request.request_id,
                run_id=snapshot.run_id,
                latest_sequence=snapshot.sequence,
            )
        )
        cursor = request.after_sequence
        replay = service.events(cursor)
        if replay:
            self._write_message(EventBatchMessage(events=replay))
            cursor = max(event.sequence for event in replay)
        while True:
            events = service.wait_for_events(cursor, timeout=1.0)
            if not events:
                time.sleep(0.05)
                continue
            for event in events:
                self._write_message(EventMessage(event=event))
                cursor = event.sequence

    def _write_message(self, message: BaseModel) -> None:
        payload = message.model_dump_json()
        self.wfile.write(payload.encode() + b"\n")
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
        self._client_subscribed = threading.Event()
        self._client_disconnected = threading.Event()

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self._server = _UnixServer(self.path, self.service)
        self._server.client_subscribed = self._client_subscribed  # type: ignore[attr-defined]
        self._server.client_disconnected = self._client_disconnected  # type: ignore[attr-defined]
        os.chmod(self.path, 0o600)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="vibeserve-supervision-server",
            daemon=True,
        )
        self._thread.start()

    def wait_for_subscriber(self, timeout: float) -> bool:
        """Wait until the presentation client has established its event stream."""
        return self._client_subscribed.wait(timeout)

    def wait_for_subscriber_disconnect(self) -> None:
        """Keep terminal events queryable until the attached client exits."""
        self._client_disconnected.wait()

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
