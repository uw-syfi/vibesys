"""Local JSONL transport for presentation clients."""

from __future__ import annotations

import errno
import logging
import sys
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING

from server.api.protocol import (
    MAX_SERVER_MESSAGE_BYTES,
    EventBatchMessage,
    ProtocolErrorMessage,
    Response,
    SubscribedMessage,
    SubscribeRequest,
)
from server.transport.subscriptions import (
    DeliveryWatermark,
    SubscriptionAdmissionClosedError,
    SubscriptionTracker,
)
from vs_project.api import CONTROL_HELLO_MAX_BYTES
from vs_sim.api import WorkerStartRefusedError

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pydantic import BaseModel

    from server.api.service import RunApi, SubscriptionBootstrap
    from server.transport.subscriptions import StreamDelivery
    from vs_project.api import OperatorKey
    from vs_sim.api import Connection, Event, Listener, Network, Threads, Worker

# Polling slack on the teardown path. After the last client hangs up, the
# server exits only once the stream loop notices the closed peer and the
# ``RECONNECT_SETTLE_SECONDS`` window elapses. The sum must stay under the
# launcher's 2s backend exit grace, or a deliberate quit is reported as a hung
# backend and SIGTERMed.
_DISCONNECT_POLL_SECONDS = 0.1
# Pause between idle checks of a stream whose cursor has not moved.
_IDLE_BACKOFF_SECONDS = 0.05
# Bound how long the accept worker can stay inside a platform's ``accept``
# after another thread closes the listener. POSIX does not promise that closing
# a descriptor in one thread interrupts a syscall already using it in another.
_ACCEPT_CLOSE_POLL_SECONDS = 0.1
# Pause before retrying `accept()` after a transient failure (e.g. the process
# or system file-descriptor table is exhausted), so a sustained shortage does
# not spin the accept loop instead of giving fds a chance to free up.
_ACCEPT_RETRY_BACKOFF_SECONDS = 0.1
MAX_SOCKET_PATH_BYTES = 103 if sys.platform == "darwin" else 107
_RECV_BYTES = 65536
_SEND_BOUND_SECONDS = 30.0
"""How long one message may wait for a client to make room for it before the server hangs up.

A client that stops reading fills its socket buffer, and an unbounded send would then hold
its session worker, and its subscription, for ever: an attached server waits for every
subscriber to leave, so it would never exit. The WebSocket gateway's analogue is
``TransportLimits.write_deadline_seconds``.
"""
MAX_REQUEST_LINE_BYTES = 1024 * 1024
"""The longest request line a session buffers; the WebSocket gateway's inbound frame cap."""
HELLO_TIMEOUT_SECONDS = 2.0
"""How long after it is accepted a connection may take to present the whole operator key.

One deadline for the whole hello, however it is split, so a peer that trickles one byte at
a time is refused at the same time as a silent one. Every operator client sends its hello
as its first write after connecting (``vs_project.api.OperatorDialer``), so it needs only
the time a local write takes; the bound is short because it is also how long a peer
without the key holds a slot.
"""
MAX_UNADMITTED_SESSIONS = 8
"""How many connections may wait to present the key at once.

When every slot is taken, accepting another connection closes the one that has waited
longest, rather than the new one. Peers without the key therefore cannot lock the
operator out by keeping the slots full: the operator's connection presents its hello in
its first write, and is admitted unless this many connections arrive after it before the
server reads that write. With :data:`HELLO_TIMEOUT_SECONDS`, the slots bound what such
peers hold: at most this many workers, each for at most that long.
"""

_LOG = logging.getLogger(__name__)


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


class RequestLineTooLongError(ValueError):
    """A request line ran past ``MAX_REQUEST_LINE_BYTES`` without its newline."""

    diagnostic_code = "request_too_large"

    def __init__(self) -> None:
        """Name the bound the line exceeded."""
        super().__init__(
            f"a request line is longer than {MAX_REQUEST_LINE_BYTES} bytes; "
            "the rest of it was discarded"
        )


class ControlKeyRefusedError(PermissionError):
    """A connection did not open with the operator key's hello; the server hung up."""

    diagnostic_code = "control_key_refused"

    def __init__(self) -> None:
        """Say what the socket needs and the likely cause, never what it got."""
        super().__init__(
            "this control socket serves only the operator: the first line was not the "
            "operator key's hello (vs_project.api.OperatorKey.hello). If you are the operator, "
            "your client most likely read a different key than the server's: check that both "
            "run with the same XDG_RUNTIME_DIR, since the key lives in its vibesys directory"
        )


class _PeerGoneError(Exception):
    """The client's end of the connection is gone; nothing more can reach it."""


@dataclass(eq=False)
class _OwnedClientSession:
    """One admitted connection and the worker the server must drain."""

    connection: Connection
    worker_ready: Event
    worker: Worker | None = None
    owner: object | None = None
    accepted: int = 0
    """The order the server accepted it in: the lowest waiting one is evicted first."""
    admitted: bool = False
    """Whether the connection presented the key; written under the server's guard."""
    evicted: bool = False
    """Whether the server closed it unadmitted to make room; written under the guard."""


class _ClientSession:
    """One client connection: read JSONL requests, answer each, or stream events."""

    def __init__(
        self,
        connection: Connection,
        api: RunApi,
        subscriptions: SubscriptionTracker,
        threads: Threads,
        key: OperatorKey,
    ) -> None:
        self._connection = connection
        self._key = key
        self._pending = b""
        self._api = api
        self._subscriptions = subscriptions
        self._threads = threads
        self._hangup_watcher: Worker | None = None

    def refuse(self) -> None:
        """Answer a connection that did not present the key with ``control_key_refused``."""
        with suppress(_PeerGoneError):
            self._write_message(
                ProtocolErrorMessage.from_exception(
                    ControlKeyRefusedError(),
                    operation="Control socket admission",
                    code=ControlKeyRefusedError.diagnostic_code,
                )
            )

    def handle(self) -> None:
        """Serve requests until the client hangs up, or until its subscription ends.

        Called only once :meth:`admit` is true: nothing is served before the client
        presents the operator key.
        """
        api = self._api
        for line in self._lines():
            request = (
                api.failure("unknown", "", RequestLineTooLongError())
                if line is None
                else api.decode_request(line)
            )
            if isinstance(request, Response):
                self._write_message(request)
                continue
            try:
                if isinstance(request, SubscribeRequest):
                    try:
                        with self._subscriptions.track() as delivery:
                            try:
                                self._stream(request, delivery)
                            except _PeerGoneError:
                                pass
                            except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010252 [BLE001]; arbitrary event serialization failures are returned as protocol stream errors.
                                self._write_stream_error(request.request_id, request.client_id, exc)
                    except SubscriptionAdmissionClosedError as exc:
                        self._write_stream_error(request.request_id, request.client_id, exc)
                    return
                response = api.execute(request)
            except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-010253 [BLE001]; the socket boundary converts request failures into typed protocol responses.
                response = api.failure(request.request_id, request.client_id, exc)
            self._write_message(response)

    def admit(self) -> bool:
        """Read the first line, at most ``CONTROL_HELLO_MAX_BYTES``; whether it is the key's hello.

        The whole line must arrive within ``HELLO_TIMEOUT_SECONDS`` of this call. Bytes
        after the hello stay buffered for :meth:`_lines`, so a client may send its first
        request in the same write.
        """
        deadline = self._threads.now() + HELLO_TIMEOUT_SECONDS
        received = b""
        while b"\n" not in received:
            remaining = deadline - self._threads.now()
            if len(received) >= CONTROL_HELLO_MAX_BYTES or remaining <= 0:
                return False
            try:
                chunk = self._connection.recv(CONTROL_HELLO_MAX_BYTES - len(received), remaining)
            except OSError:
                return False
            if not chunk:
                return False
            received += chunk
        hello, _, self._pending = received.partition(b"\n")
        return self._key.admits(hello + b"\n")

    def _lines(self) -> Iterator[bytes | None]:
        """Yield each newline-terminated request, then any unterminated rest at end of stream.

        A line that grows past ``MAX_REQUEST_LINE_BYTES`` yields ``None`` once,
        and its remaining bytes up to the next newline are dropped unbuffered,
        so a client cannot make a session hold an unbounded line. A read error
        ends the stream like an end of file: either way the client can send
        nothing more.
        """
        pending = b""
        discarding = False
        for chunk in self._chunks():
            data = chunk
            if discarding:
                newline = data.find(b"\n")
                if newline < 0:
                    continue
                discarding = False
                data = data[newline + 1 :]
            pending += data
            *lines, pending = pending.split(b"\n")
            for line in lines:
                yield line + b"\n" if len(line) < MAX_REQUEST_LINE_BYTES else None
            if len(pending) >= MAX_REQUEST_LINE_BYTES:
                pending = b""
                discarding = True
                yield None
        if pending and not discarding:
            yield pending

    def _chunks(self) -> Iterator[bytes]:
        """What the client sent after its hello, then each read, until end of stream or error."""
        if self._pending:
            yield self._pending
        while True:
            try:
                chunk = self._connection.recv(_RECV_BYTES)
            except OSError:
                return
            if not chunk:
                return
            yield chunk

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
            # Keep both fields in the same event-store generation.
            snapshot = api.snapshot()
            self._write_message(
                SubscribedMessage(
                    request_id=request.request_id,
                    client_id=request.client_id,
                    run_id=snapshot.run_id,
                    latest_sequence=snapshot.sequence,
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
        delivery.delivered(DeliveryWatermark(store_id=store_id, sequence=cursor))
        hung_up = self._threads.event()
        self._hangup_watcher = self._threads.spawn(
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
                cursor, reported_floor, store_id = self._rebootstrap(request, cursor, store_id)
                delivery.delivered(DeliveryWatermark(store_id=store_id, sequence=cursor))
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
                cursor, reported_floor, store_id = self._rebootstrap(request, cursor, store_id)
                delivery.delivered(DeliveryWatermark(store_id=store_id, sequence=cursor))
                continue
            self._write_event_batch(
                EventBatchMessage(
                    events=checkpoint.events,
                    through_sequence=checkpoint.through_sequence,
                    active_executions=checkpoint.active_executions,
                    history_after_sequence=reported_floor,
                    store_id=store_id,
                )
            )
            cursor = checkpoint.through_sequence
            delivery.delivered(DeliveryWatermark(store_id=store_id, sequence=cursor))

    def close(self) -> None:
        """Close the connection and drain the subscription's hangup watcher."""
        self._connection.close()
        if self._hangup_watcher is not None:
            self._hangup_watcher.join()

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
        finally:
            hung_up.set()

    def _rebootstrap(
        self, request: SubscribeRequest, cursor: int, store_id: str
    ) -> tuple[int, int, str]:
        """Restart this subscription's replay against the journal's live state.

        The replay resumes from what this stream delivered (``cursor`` in
        ``store_id``), not from the subscribe request: a swapped store makes the
        cursor stale whether or not the request named its store.
        """
        return self._write_bootstrap(
            request,
            self._api.subscription_bootstrap(cursor, request.tail, store_id=store_id),
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
        self._write_event_batch(
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

    def _write_event_batch(self, batch: EventBatchMessage) -> None:
        """Write the same bounded stream-message sequence as the WebSocket gateway."""
        for message in batch.stream_messages_within(MAX_SERVER_MESSAGE_BYTES):
            self._write_message(message)

    def _write_stream_error(self, request_id: str, client_id: str, error: Exception) -> None:
        """Report a replay or stream failure without hiding a live connection."""
        protocol_error = ProtocolErrorMessage.from_exception(
            error,
            operation="Event stream",
            code=(
                error.diagnostic_code
                if isinstance(error, SubscriptionAdmissionClosedError)
                else "stream_failed"
            ),
            request_id=request_id,
        ).model_copy(update={"client_id": client_id})
        with suppress(_PeerGoneError):
            self._write_message(protocol_error)

    def _write_message(self, message: BaseModel) -> None:
        payload = message.model_dump_json()
        try:
            self._connection.send(payload.encode() + b"\n", _SEND_BOUND_SECONDS)
        except TimeoutError as exc:
            # Part of the message may have gone out, so the stream cannot continue.
            _LOG.warning(
                "a client read nothing for %g s while the server was writing to it; "
                "disconnecting it",
                _SEND_BOUND_SECONDS,
            )
            raise _PeerGoneError from exc
        except OSError as exc:
            raise _PeerGoneError from exc


class UnixJsonlServer:
    """Own a private Unix socket serving one or more concurrent clients that hold the key.

    Each client is served on its own worker. A client that hangs up at any
    point, including between its request and the response, ends only its own
    session; the server neither reports it nor stops serving the others.
    """

    def __init__(  # noqa: PLR0913  # lint-waiver: LW-313304 [PLR0913]; the key is a required, separate admission input; folding it into the network would make a listener that dials, into the api would make every transport (the WebSocket gateway has its own capability) carry it, and a parameter object would only rename these six independent inputs.
        self,
        path: Path,
        api: RunApi,
        subscriptions: SubscriptionTracker | None,
        threads: Threads,
        network: Network,
        *,
        key: OperatorKey,
    ) -> None:
        """Create a Unix server, optionally sharing subscription accounting.

        ``key`` is the operator key every connection must present first
        (``vs_project.api.OperatorKey``); the socket's ``0600`` mode does not keep out an
        agent that runs as the operator's uid.

        ``threads`` runs the accept loop and each client, and paces idle
        streams; ``network`` binds the socket. A client that leaves a message
        unsent for ``_SEND_BOUND_SECONDS`` is disconnected, so it holds neither
        its worker nor the server's exit.
        """
        self.path = path
        self.api = api
        self._threads = threads
        self._subscriptions = subscriptions or SubscriptionTracker(threads=threads)
        self._network = network
        self._key = key
        self._listener: Listener | None = None
        self._acceptor: Worker | None = None
        self._guard = self._threads.lock()
        self._sessions: set[_OwnedClientSession] = set()
        self._accepted = 0
        self._thread_identity = self._threads.local(object)
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
                connection = listener.accept(timeout=_ACCEPT_CLOSE_POLL_SECONDS)
            except TimeoutError:
                # A close may not interrupt an in-flight accept on every Unix.
                # Poll only to observe that lifecycle transition; an idle,
                # still-open listener simply starts another bounded accept.
                with self._guard:
                    if self._closing:
                        return
                continue
            except OSError as exc:
                with self._guard:
                    closing = self._closing
                if closing:
                    return  # the listener was closed deliberately
                # A transient failure, e.g. EMFILE/ENFILE while the process or
                # system file-descriptor table is exhausted. The listener is
                # still open: log it and keep accepting once the pressure
                # eases, rather than permanently killing the accept loop.
                _LOG.warning("accept() failed (errno=%s): %s; retrying", exc.errno, exc)
                self._threads.sleep(_ACCEPT_RETRY_BACKOFF_SECONDS)
                continue
            with self._guard:
                if self._closing:
                    connection.close()
                    return
                session, evicted = self._track_locked(connection)
            if evicted is not None:
                # Its worker's read of the hello fails, and the worker returns unadmitted.
                evicted.close()
            if not self._start_worker(session):
                return

    def _start_worker(self, session: _OwnedClientSession) -> bool:
        """Serve ``session`` on its own worker; ``False`` when the server is closing."""
        connection = session.connection
        try:
            worker = self._threads.spawn(
                partial(self._serve, session),
                name="vibesys-server-jsonl-client",
                daemon=True,
            )
        except WorkerStartRefusedError as exc:
            with self._guard:
                self._sessions.discard(session)
                closing = self._closing
            connection.close()
            session.worker_ready.set()
            if closing:
                return False
            _LOG.warning("failed to spawn client worker thread: %s; retrying", exc)
            self._threads.sleep(_ACCEPT_RETRY_BACKOFF_SECONDS)
            return True
        except BaseException:
            with self._guard:
                self._sessions.discard(session)
            connection.close()
            session.worker_ready.set()
            raise
        with self._guard:
            session.worker = worker
        session.worker_ready.set()
        return True

    def _track_locked(
        self, connection: Connection
    ) -> tuple[_OwnedClientSession, Connection | None]:
        """Track ``connection`` for a worker; return it and the oldest waiting one it evicts.

        When the unadmitted slots are full, the connection that has waited longest is
        marked evicted, and the caller closes it once it has released the guard.

        Closing the newcomer instead would let peers that reconnect as soon as they are
        refused keep every slot, and the operator out, for as long as they like.
        """
        self._discard_finished_sessions_locked()
        waiting = [owned for owned in self._sessions if not owned.admitted and not owned.evicted]
        evicted: Connection | None = None
        if len(waiting) >= MAX_UNADMITTED_SESSIONS:
            oldest = min(waiting, key=lambda owned: owned.accepted)
            oldest.evicted = True
            evicted = oldest.connection
        self._accepted += 1
        session = _OwnedClientSession(connection, self._threads.event(), accepted=self._accepted)
        self._sessions.add(session)
        return session, evicted

    def _serve(self, owned: _OwnedClientSession) -> None:
        owned.worker_ready.wait()
        owned.owner = self._thread_identity.get()
        session = _ClientSession(
            owned.connection, self.api, self._subscriptions, self._threads, self._key
        )
        try:
            if not session.admit():
                session.refuse()
                return
            with self._guard:
                if owned.evicted:
                    return
                owned.admitted = True
            session.handle()
        except _PeerGoneError:
            pass
        finally:
            session.close()

    def _discard_finished_sessions_locked(self) -> None:
        """Forget workers only after they have completely returned."""
        self._sessions = {
            session
            for session in self._sessions
            if session.worker is None or session.worker.is_alive()
        }

    def client_count(self) -> int:
        """How many clients hold a connection now, admitted or not."""
        with self._guard:
            self._discard_finished_sessions_locked()
            return len(self._sessions)

    def wait_for_subscriber(self, timeout: float) -> bool:
        """Wait until a presentation client has established its event stream."""
        return self._subscriptions.wait_for_subscriber(timeout)

    def wait_until_delivered(self, watermark: DeliveryWatermark, timeout: float) -> bool:
        """Close subscription admission and wait for streams to reach *watermark*."""
        return self._subscriptions.wait_until_delivered(watermark, timeout)

    def wait_for_subscriber_disconnect(self) -> None:
        """Keep terminal events queryable until the last active subscriber exits."""
        self._subscriptions.wait_for_none_active()

    def close(self) -> None:
        """Stop admission, hang up on every client, and drain their workers.

        A request may call ``close`` reentrantly. A client worker cannot drain
        itself, and two such callers must not cross-join each other, so a
        reentrant call initiates shutdown and then unwinds through the normal
        worker-finalization path. An outside owner drains every worker.
        """
        caller = self._thread_identity.get()
        with self._guard:
            self._closing = True
            listener = self._listener
            acceptor = self._acceptor
            self._discard_finished_sessions_locked()
            sessions = tuple(self._sessions)
            reentrant = any(session.owner is caller for session in sessions)
        if listener is not None:
            listener.close()
        for session in sessions:
            session.connection.close()
        if acceptor is not None:
            acceptor.join()
        if reentrant:
            return
        for session in sessions:
            session.worker_ready.wait()
            worker = session.worker
            if worker is not None:
                worker.join()
        with self._guard:
            self._discard_finished_sessions_locked()

    def __enter__(self) -> UnixJsonlServer:
        """Start the server and return it as a context manager."""
        self.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        """Close the server when leaving its context."""
        self.close()
