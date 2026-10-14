"""Local JSONL transport for presentation clients."""

from __future__ import annotations

import errno
import sys
from contextlib import suppress
from functools import partial
from typing import TYPE_CHECKING

from server.api.protocol import (
    EventBatchMessage,
    ProtocolErrorMessage,
    Response,
    SubscribedMessage,
    SubscribeRequest,
    decode_request,
)
from server.transport.subscriptions import SubscriptionTracker
from vs_sim.api import OsThreads, UnixNetwork

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pydantic import BaseModel

    from server.api.service import RunApi, SubscriptionBootstrap
    from server.transport.subscriptions import StreamDelivery
    from vs_sim.api import Connection, Event, Listener, Network, Threads, Worker

# Polling slack on the teardown path. After the last client hangs up, the
# server exits only once the stream loop notices the closed peer and the
# ``RECONNECT_SETTLE_SECONDS`` window elapses. The sum must stay under the
# launcher's 2s backend exit grace, or a deliberate quit is reported as a hung
# backend and SIGTERMed.
_DISCONNECT_POLL_SECONDS = 0.1
# Pause between idle checks of a stream whose cursor has not moved.
_IDLE_BACKOFF_SECONDS = 0.05
MAX_SOCKET_PATH_BYTES = 103 if sys.platform == "darwin" else 107
_RECV_BYTES = 65536


class SocketPathTooLongError(OSError):
    """A server transport path does not fit in ``sockaddr_un.sun_path``."""

    def __init__(self, path: Path, limit: int) -> None:
        """Describe the encoded path and platform byte limit."""
        encoded = len(str(path).encode())
        super().__init__(
            errno.ENAMETOOLONG,
            f"Unix socket path is {encoded} bytes, over this platform's "
            f"{limit}-byte limit: {path}. Choose a shorter directory.",
        )
        self.path = path
        self.limit = limit


def validate_socket_path(path: Path) -> Path:
    """Return a bindable transport path or raise a precise path error."""
    if len(str(path).encode()) > MAX_SOCKET_PATH_BYTES:
        raise SocketPathTooLongError(path, MAX_SOCKET_PATH_BYTES)
    return path


class _PeerGoneError(Exception):
    """The client's end of the connection is gone; nothing more can reach it."""


class _ClientSession:
    """One client connection: read JSONL requests, answer each, or stream events."""

    def __init__(
        self,
        connection: Connection,
        api: RunApi,
        subscriptions: SubscriptionTracker,
        threads: Threads,
    ) -> None:
        self._connection = connection
        self._api = api
        self._subscriptions = subscriptions
        self._threads = threads

    def handle(self) -> None:
        """Serve requests until the client hangs up, or until its subscription ends."""
        api = self._api
        for line in self._lines():
            request = decode_request(line)
            if isinstance(request, Response):
                self._write_message(request)
                continue
            try:
                if isinstance(request, SubscribeRequest):
                    with self._subscriptions.track() as delivery:
                        try:
                            self._stream(request, delivery)
                        except _PeerGoneError:
                            pass
                        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010252 [BLE001]; arbitrary event serialization failures are returned as protocol stream errors.
                            self._write_stream_error(request.request_id, request.client_id, exc)
                    return
                response = api.execute(request)
            except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010253 [BLE001]; the socket boundary converts request failures into typed protocol responses.
                response = Response.from_exception(
                    request.request_id,
                    exc,
                    operation="Request",
                ).model_copy(update={"client_id": request.client_id})
            self._write_message(response)

    def _lines(self) -> Iterator[bytes]:
        """Yield each newline-terminated request, then any unterminated rest at end of stream.

        A read error ends the stream like an end of file: either way the client
        can send nothing more.
        """
        pending = b""
        while True:
            try:
                chunk = self._connection.recv(_RECV_BYTES)
            except OSError:
                chunk = b""
            if not chunk:
                if pending:
                    yield pending
                return
            pending += chunk
            *lines, pending = pending.split(b"\n")
            for line in lines:
                yield line + b"\n"

    def _stream(self, request: SubscribeRequest, delivery: StreamDelivery) -> None:
        api = self._api
        try:
            bootstrap = api.subscription_bootstrap(
                request.after_sequence, request.tail, store_id=request.store_id
            )
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
                    client_id=request.client_id,
                    run_id=api.snapshot().run_id,
                    latest_sequence=api.latest_sequence,
                )
            )
            raise
        self._write_message(
            SubscribedMessage(
                request_id=request.request_id,
                client_id=request.client_id,
                run_id=bootstrap.run_id,
                latest_sequence=bootstrap.through_sequence,
            )
        )
        cursor, reported_floor, store_id = self._write_bootstrap(request, bootstrap)
        delivery.delivered(cursor)
        hung_up = self._threads.event()
        self._threads.spawn(
            partial(self._watch_for_hangup, hung_up), name="vibesys-server-jsonl-hangup"
        )
        while True:
            if not api.wait_for_change(cursor, timeout=_DISCONNECT_POLL_SECONDS):
                if hung_up.is_set():
                    return
                self._threads.sleep(_IDLE_BACKOFF_SECONDS)
                continue
            if request.tail is not None and api.latest_sequence - cursor > request.tail:
                # More live output landed in one wait than the tail bound was
                # willing to replay. Bootstrap again at a fresh tail rather
                # than deliver a window the bound was meant to exclude.
                cursor, reported_floor, store_id = self._rebootstrap(request)
                delivery.delivered(cursor)
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
                delivery.delivered(cursor)
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
            delivery.delivered(cursor)

    def _watch_for_hangup(self, hung_up: Event) -> None:
        """Read and discard what the client sends after subscribing; set ``hung_up`` at its end.

        A subscription owns its connection and reads no further requests. The
        bytes still have to be read: a peer that closed with bytes unread is
        not yet at end of stream, so an idle stream that only checked for end
        of stream would never see the hangup, and an idle run writes nothing
        that could fail instead.
        """
        try:
            while self._connection.recv(_RECV_BYTES):
                pass
        except OSError:
            pass
        hung_up.set()

    def _rebootstrap(self, request: SubscribeRequest) -> tuple[int, int, str]:
        """Restart this subscription's replay against the journal's live state."""
        api = self._api
        return self._write_bootstrap(
            request,
            api.subscription_bootstrap(
                request.after_sequence, request.tail, store_id=request.store_id
            ),
            rebootstrap=True,
        )

    def _write_bootstrap(
        self,
        request: SubscribeRequest,
        bootstrap: SubscriptionBootstrap,
        *,
        rebootstrap: bool = False,
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
                rebootstrap=rebootstrap,
            )
        )
        return bootstrap.through_sequence, reported_floor, bootstrap.store_id

    def _write_stream_error(self, request_id: str, client_id: str, error: Exception) -> None:
        """Report a replay or stream failure without hiding a live connection."""
        protocol_error = ProtocolErrorMessage.from_exception(
            error, operation="Event stream", code="stream_failed", request_id=request_id
        ).model_copy(update={"client_id": client_id})
        with suppress(_PeerGoneError):
            self._write_message(protocol_error)

    def _write_message(self, message: BaseModel) -> None:
        payload = message.model_dump_json()
        try:
            self._connection.send(payload.encode() + b"\n")
        except OSError as exc:
            raise _PeerGoneError from exc


class UnixJsonlServer:
    """Own a private Unix socket serving one or more concurrent clients.

    Each client is served on its own worker. A client that hangs up at any
    point, including between its request and the response, ends only its own
    session; the server neither reports it nor stops serving the others.
    """

    def __init__(
        self,
        path: Path,
        api: RunApi,
        subscriptions: SubscriptionTracker | None = None,
        threads: Threads | None = None,
        network: Network | None = None,
    ) -> None:
        """Create a Unix server, optionally sharing subscription accounting.

        ``threads`` runs the accept loop and each client, and paces idle
        streams; ``network`` binds the socket. Both default to the operating
        system's.
        """
        self.path = path
        self.api = api
        self._threads = threads or OsThreads()
        self._subscriptions = subscriptions or SubscriptionTracker()
        self._network = network or UnixNetwork()
        self._listener: Listener | None = None
        self._acceptor: Worker | None = None
        self._guard = self._threads.lock()
        self._open: set[Connection] = set()
        self._closing = False

    def start(self) -> None:
        """Create the private Unix socket and start serving clients."""
        validate_socket_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        listener = self._network.listen(str(self.path))
        try:
            # The socket is a file only on a filesystem network; any other has nothing to restrict.
            with suppress(FileNotFoundError):
                self.path.chmod(0o600)
            self._acceptor = self._threads.spawn(
                partial(self._accept_loop, listener), name="vibesys-server-jsonl", daemon=True
            )
        except BaseException:
            listener.close()
            raise
        self._listener = listener

    def _accept_loop(self, listener: Listener) -> None:
        while True:
            try:
                connection = listener.accept()
            except OSError:
                return  # the listener was closed
            with self._guard:
                if self._closing:
                    connection.close()
                    return
                self._open.add(connection)
            self._threads.spawn(
                partial(self._serve, connection), name="vibesys-server-jsonl-client", daemon=True
            )

    def _serve(self, connection: Connection) -> None:
        try:
            _ClientSession(connection, self.api, self._subscriptions, self._threads).handle()
        except _PeerGoneError:
            pass
        finally:
            with self._guard:
                self._open.discard(connection)
            connection.close()

    def wait_for_subscriber(self, timeout: float) -> bool:
        """Wait until a presentation client has established its event stream."""
        return self._subscriptions.wait_for_subscriber(timeout)

    def wait_until_delivered(self, sequence: int, timeout: float) -> bool:
        """Wait, bounded, until every open stream wrote through ``sequence``."""
        return self._subscriptions.wait_until_delivered(sequence, timeout)

    def wait_for_subscriber_disconnect(self) -> None:
        """Keep terminal events queryable until the last active subscriber exits."""
        self._subscriptions.wait_for_none_active()

    def close(self) -> None:
        """Stop accepting, hang up on every client, and remove the socket path."""
        listener, self._listener = self._listener, None
        acceptor, self._acceptor = self._acceptor, None
        with self._guard:
            self._closing = True
            open_connections, self._open = self._open, set()
        if listener is not None:
            listener.close()
        if acceptor is not None:
            acceptor.join(timeout=2)
        for connection in open_connections:
            connection.close()

    def __enter__(self) -> UnixJsonlServer:
        """Start the server and return it as a context manager."""
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Close the server when leaving its context."""
        self.close()
