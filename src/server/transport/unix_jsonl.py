"""Local JSONL transport for presentation clients."""

from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
from contextlib import suppress
from pathlib import Path  # noqa: TC003  # tracked: #288
from typing import TYPE_CHECKING

from pydantic import BaseModel, TypeAdapter

from server.api.protocol import (
    EventBatchMessage,
    ProtocolErrorMessage,
    ProtocolRequest,
    Response,
    SubscribedMessage,
    SubscribeRequest,
)
from server.transport.subscriptions import SubscriptionTracker
from vibesys.unix_socket import validate_socket_path

if TYPE_CHECKING:
    from server.api.service import RunApi, SubscriptionBootstrap

_REQUEST_ADAPTER = TypeAdapter(ProtocolRequest)

# Polling slack on the teardown path. After the last client hangs up, the
# server exits only once the stream loop notices the closed peer, the
# ``RECONNECT_SETTLE_SECONDS`` window elapses, and ``serve_forever`` observes
# ``shutdown``. The sum must stay under the launcher's 2s backend exit grace,
# or a deliberate quit is reported as a hung backend and SIGTERMed. Both polls
# were 1.0s and 0.5s (the ``socketserver`` default), which with the settle
# window overran that grace.
_DISCONNECT_POLL_SECONDS = 0.1
_SHUTDOWN_POLL_SECONDS = 0.1


class _RequestHandler(socketserver.StreamRequestHandler):
    server: _JsonlUnixServer

    def handle(self) -> None:
        api = self.server.api
        for line in self.rfile:
            request_id = "unknown"
            try:
                raw = json.loads(line)
                request_id = str(raw.get("request_id", request_id))
                request = _REQUEST_ADAPTER.validate_python(raw)
                if isinstance(request, SubscribeRequest):
                    with self.server.subscriptions.track():
                        try:
                            self._stream(request)
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        except Exception as exc:  # noqa: BLE001  # tracked: #288
                            self._write_stream_error(request.request_id, exc)
                    return
                response = api.execute(request)
            except Exception as exc:  # noqa: BLE001  # tracked: #288
                response = Response.from_exception(
                    request_id,
                    exc,
                    operation="Request",
                )
            self.wfile.write(response.model_dump_json().encode() + b"\n")
            self.wfile.flush()

    def _stream(self, request: SubscribeRequest) -> None:
        api = self.server.api
        try:
            bootstrap = api.subscription_bootstrap(request.after_sequence, request.tail)
        except Exception:
            # A bootstrap failure must not reject the dial: the client probes
            # ``tail`` support by dialing and treats a pre-handshake failure
            # as a server without the field, so it would retry the whole
            # history against the same fault. Acknowledge the accepted
            # subscribe first; the failure then reaches the client as a
            # stream error, exactly as it did when the replay was read after
            # the handshake.
            self._write_message(
                SubscribedMessage(
                    request_id=request.request_id,
                    run_id=api.snapshot().run_id,
                    latest_sequence=api.latest_sequence,
                )
            )
            raise
        self._write_message(
            SubscribedMessage(
                request_id=request.request_id,
                run_id=bootstrap.run_id,
                latest_sequence=bootstrap.through_sequence,
            )
        )
        cursor, reported_floor, store_id = self._write_bootstrap(request, bootstrap)
        while True:
            if not api.wait_for_change(cursor, timeout=_DISCONNECT_POLL_SECONDS):
                if self._client_disconnected():
                    return
                time.sleep(0.05)
                continue
            if request.tail is not None and api.latest_sequence - cursor > request.tail:
                # More live output landed in one wait than the tail bound was
                # willing to replay. Bootstrap again at a fresh tail rather
                # than deliver a window the bound was meant to exclude.
                cursor, reported_floor, store_id = self._rebootstrap(request)
                continue
            # ``wait_for_change`` only tells us that the stream changed. Take
            # one watermark-consistent snapshot before writing so a resumed
            # run, or a burst of live output, reaches the client as one state
            # transition instead of thousands of repaint-triggering messages.
            checkpoint = api.subscription_checkpoint(cursor, store_id=store_id)
            if checkpoint.store_id != store_id:
                # The run's durable event store was attached after this client
                # subscribed. The cursor numbers the retired store, so nothing
                # after it is a continuation of what the client folded, however
                # the two logs compare in length. Bootstrap against the store
                # that is live now; the client re-folds from the batch's id.
                cursor, reported_floor, store_id = self._rebootstrap(request)
                continue
            self._write_message(
                EventBatchMessage(
                    events=checkpoint.events,
                    through_sequence=checkpoint.through_sequence,
                    active_executions=checkpoint.active_executions,
                    history_after_sequence=reported_floor,
                    store_id=store_id,
                )
            )
            cursor = checkpoint.through_sequence

    def _rebootstrap(self, request: SubscribeRequest) -> tuple[int, int, str]:
        """Restart this subscription's replay against the journal's live state."""
        api = self.server.api
        return self._write_bootstrap(
            request, api.subscription_bootstrap(request.after_sequence, request.tail)
        )

    def _write_bootstrap(
        self,
        request: SubscribeRequest,
        bootstrap: SubscriptionBootstrap,
    ) -> tuple[int, int, str]:
        """Send one tail-bounded replay batch; return the cursor, floor, and store.

        Without ``tail`` the reported floor stays 0: the client asked for
        everything from its own cursor onward, so nothing was withheld and old
        clients see the field's default.
        """
        reported_floor = 0 if request.tail is None else bootstrap.floor
        self._write_message(
            EventBatchMessage(
                events=bootstrap.events,
                through_sequence=bootstrap.through_sequence,
                active_executions=bootstrap.active_executions,
                history_after_sequence=reported_floor,
                store_id=bootstrap.store_id,
            )
        )
        return bootstrap.through_sequence, reported_floor, bootstrap.store_id

    def _write_stream_error(self, request_id: str, error: Exception) -> None:
        """Report a replay or stream failure without hiding a live connection."""
        protocol_error = ProtocolErrorMessage.from_exception(
            error,
            operation="Event stream",
            code="stream_failed",
            request_id=request_id,
        )
        with suppress(BrokenPipeError, ConnectionResetError):
            self._write_message(protocol_error)

    def _client_disconnected(self) -> bool:
        try:
            return self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True

    def _write_message(self, message: BaseModel) -> None:
        payload = message.model_dump_json()
        self.wfile.write(payload.encode() + b"\n")
        self.wfile.flush()


class _JsonlUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(  # noqa: ANN204  # tracked: #288
        self,
        path: Path,
        api: RunApi,
        subscriptions: SubscriptionTracker,
    ):
        self.api = api
        self.subscriptions = subscriptions
        super().__init__(str(path), _RequestHandler)


class UnixJsonlServer:
    """Own a private Unix socket serving one or more concurrent clients."""

    def __init__(self, path: Path, api: RunApi):  # noqa: ANN204, D107  # tracked: #288
        self.path = path
        self.api = api
        self._server: _JsonlUnixServer | None = None
        self._thread: threading.Thread | None = None
        self._subscriptions = SubscriptionTracker()

    def start(self) -> None:  # noqa: D102  # tracked: #288
        validate_socket_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        self._server = _JsonlUnixServer(self.path, self.api, self._subscriptions)
        os.chmod(self.path, 0o600)  # noqa: PTH101  # tracked: #288
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": _SHUTDOWN_POLL_SECONDS},
            name="vibesys-server-jsonl",
            daemon=True,
        )
        self._thread.start()

    def wait_for_subscriber(self, timeout: float) -> bool:
        """Wait until a presentation client has established its event stream."""
        return self._subscriptions.wait_for_subscriber(timeout)

    def wait_for_subscriber_disconnect(self) -> None:
        """Keep terminal events queryable until the last active subscriber exits."""
        self._subscriptions.wait_for_none_active()

    def close(self) -> None:  # noqa: D102  # tracked: #288
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> UnixJsonlServer:  # noqa: D105  # tracked: #288
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:  # noqa: D105  # tracked: #288
        self.close()
