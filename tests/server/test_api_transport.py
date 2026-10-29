"""Request API and Unix JSONL transport boundary tests."""

import json
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any, Never, cast

import pytest
from tests.server.support import (
    CONTROL_KEY,
    REQUEST_TIME,
    ServerParts,
    build_server_parts,
    operator_dialer,
)

from server.api.protocol import (
    ChatQuery,
    EventsQuery,
    ProtocolRequest,
    Request,
    Response,
    RunSnapshot,
    SnapshotQuery,
    StopCommand,
    SubscribeRequest,
)
from server.api.service import RunApi
from server.chat.manager import ChatAnswer
from server.events import EventType
from server.run_lifecycle import RunStatus
from server.transport.unix_jsonl import (
    MAX_SOCKET_PATH_BYTES,
    SocketPathTooLongError,
    UnixJsonlServer,
    validate_socket_path,
)
from vs_sim.api import Connection, OsThreads, UnixNetwork
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads

type _Dial = Callable[[], Connection]


def _served[T](
    socket_dir: Path,
    log_dir: Path | None,
    body: Callable[[ServerParts, _Dial], T],
    *,
    prepare: Callable[[ServerParts], None] = lambda _parts: None,
    api_factory: Callable[[ServerParts], RunApi] = lambda parts: parts.api,
) -> T:
    """Run ``body`` against a Unix JSONL server on the simulated network and threads."""
    threads = SimThreads(schedule_seed=0)
    socket_path = socket_dir / "server.sock"

    def main() -> T:
        parts = build_server_parts(log_dir, threads=threads)
        network = SimNetwork(threads)
        try:
            prepare(parts)
            server = UnixJsonlServer(
                socket_path,
                api_factory(parts),
                threads=threads,
                network=network,
                subscriptions=None,
                key=CONTROL_KEY,
            )
            server.start()
            try:
                return body(
                    parts,
                    lambda: operator_dialer(network).connect(str(socket_path), HANG_GUARD_S),
                )
            finally:
                server.close()
        finally:
            parts.close()

    return threads.run(main)


def _send(connection: Connection, request: Request | bytes) -> None:
    frame = request if isinstance(request, bytes) else request.model_dump_json().encode()
    connection.send(frame + b"\n")


def _receive(connection: Connection) -> dict[str, Any]:
    line = b""
    while not line.endswith(b"\n"):
        chunk = connection.recv(1, HANG_GUARD_S)
        assert chunk, f"the server hung up mid-line after {line!r}"
        line += chunk
    return json.loads(line)


def _request(dial: _Dial, request: Request | bytes) -> dict[str, Any]:
    connection = dial()
    try:
        _send(connection, request)
        return _receive(connection)
    finally:
        connection.close()


def test_api_routes_chat_to_configured_handler(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    try:
        questions: list[str] = []
        parts.chat.install_default_handler(
            lambda question: (
                questions.append(question)
                or ChatAnswer(text="agent answer", invocation_id="exec-1")
            )
        )

        response = parts.api.execute(ChatQuery(timestamp=REQUEST_TIME, text="what changed?"))

        assert response.chat is not None
        assert response.chat.answer == "agent answer"
        assert questions == ["what changed?"]
        assert response.events[-1].type is EventType.CHAT
        assert response.events[-1].agent_kind == "chat"
    finally:
        parts.close()


def test_api_fallback_explains_agent_availability(tmp_path: Path) -> None:
    parts = build_server_parts(tmp_path)
    try:
        answer = parts.chat.chat("what happened in this experiment?")
        assert "chat agent is not available" in answer
        assert "not finished starting up" in answer
        assert "/history" not in answer

        parts.controller.finish()
        assert "the run has finished" in parts.chat.chat("what happened?")
    finally:
        parts.close()


def test_transport_round_trips_the_stop_command(socket_dir: Path) -> None:
    """`command.stop` parses off the wire, dispatches, and acks as pending."""

    def body(parts: ServerParts, dial: _Dial) -> None:
        response = _request(dial, StopCommand(timestamp=REQUEST_TIME, client_id="unix-client"))

        assert response["ok"] is True
        assert response["client_id"] == "unix-client"
        assert response["ack"] == {"action": "pause_now", "status": "pending"}
        assert parts.controller.run_status() is RunStatus.STOPPING

    _served(socket_dir, socket_dir / "logs", body)


def test_transport_supports_multiple_clients_and_replay(socket_dir: Path) -> None:
    status, replay = _served(
        socket_dir,
        socket_dir / "logs",
        lambda _parts, dial: (
            _request(dial, SnapshotQuery(timestamp=REQUEST_TIME)),
            _request(dial, EventsQuery(timestamp=REQUEST_TIME, after_sequence=0)),
        ),
    )

    assert status["ok"] is True
    assert status["snapshot"]["status"] == "running"
    sequences = [event["sequence"] for event in replay["events"]]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))
    assert any(event["type"] == "server_started" for event in replay["events"])


@pytest.mark.parametrize("frame", [b"[]", b"null", b'"text"', b"7", b"not json", b"\xff\xfe"])
def test_transport_rejects_a_malformed_frame_as_invalid_input(
    socket_dir: Path, frame: bytes
) -> None:
    response = _served(socket_dir, socket_dir / "logs", lambda _parts, dial: _request(dial, frame))

    assert response["ok"] is False
    assert response["request_id"] == "unknown"
    assert response["error"] == "Request received invalid input"


def test_transport_returns_sanitized_request_errors(socket_dir: Path) -> None:

    def fail_chat(question: str) -> ChatAnswer:
        _failure_message = f"token=super-secret Chat agent failed while answering: {question}"
        raise RuntimeError(_failure_message)

    response = _served(
        socket_dir,
        socket_dir / "logs",
        lambda _parts, dial: _request(
            dial, ChatQuery(timestamp=REQUEST_TIME, text="what happened?")
        ),
        prepare=lambda parts: parts.chat.install_default_handler(fail_chat),
    )

    assert response["ok"] is False
    assert response["error"] == "Request failed"
    assert response["diagnostic"]["scope"] == "request"
    assert response["diagnostic"]["detail"] == (
        "RuntimeError: token=[REDACTED] Chat agent failed while answering: what happened?"
    )


def test_subscription_streams_one_consistent_append_batch(socket_dir: Path) -> None:
    def body(parts: ServerParts, dial: _Dial) -> list[dict[str, Any]]:
        client = dial()
        _send(
            client,
            SubscribeRequest(timestamp=REQUEST_TIME, after_sequence=0, client_id="unix-subscriber"),
        )
        subscribed = _receive(client)
        replay = _receive(client)
        with parts.condition:
            parts.journal.record(EventType.CHAT, "hello", status="answered")
            parts.journal.record(EventType.STATUS_QUERY, "/history")
        streamed = _receive(client)
        client.close()
        return [subscribed, replay, streamed]

    subscribed, replay, streamed = _served(socket_dir, socket_dir / "logs", body)

    assert subscribed["type"] == "subscribed"
    assert subscribed["client_id"] == "unix-subscriber"
    assert replay["type"] == "event_batch"
    assert streamed["type"] == "event_batch"
    assert [event["type"] for event in streamed["events"]] == ["chat", "status_query"]
    assert streamed["through_sequence"] == streamed["events"][-1]["sequence"]


class _BootstrapFailureApi:
    """Run API Fake exposing which state built the failure acknowledgement."""

    def __init__(self, delegate: RunApi) -> None:
        self._delegate = delegate
        self.latest_sequence_reads = 0
        self.snapshots: list[RunSnapshot] = []

    def decode_request(self, frame: str | bytes) -> ProtocolRequest | Response:
        return self._delegate.decode_request(frame)

    def snapshot(self) -> RunSnapshot:
        snapshot = self._delegate.snapshot()
        self.snapshots.append(snapshot)
        return snapshot

    @property
    def latest_sequence(self) -> int:
        # Model a different event-store generation becoming visible between
        # two reads. The acknowledgement must not ask for this second value.
        self.latest_sequence_reads += 1
        return self._delegate.latest_sequence + 1

    def subscription_bootstrap(
        self,
        _after_sequence: int,
        _tail: int | None,
        *,
        store_id: str | None = None,
    ) -> Never:
        del store_id
        raise RuntimeError


def test_bootstrap_failure_acknowledgement_uses_one_snapshot(socket_dir: Path) -> None:
    fakes: list[_BootstrapFailureApi] = []

    def fake_api(parts: ServerParts) -> RunApi:
        fake = _BootstrapFailureApi(parts.api)
        fakes.append(fake)
        return cast("RunApi", fake)

    def body(_parts: ServerParts, dial: _Dial) -> tuple[dict[str, Any], dict[str, Any]]:
        client = dial()
        try:
            _send(client, SubscribeRequest(timestamp=REQUEST_TIME, client_id="failed-bootstrap"))
            return _receive(client), _receive(client)
        finally:
            client.close()

    subscribed, failure = _served(
        socket_dir,
        socket_dir / "logs",
        body,
        api_factory=fake_api,
    )

    fake = fakes[0]
    assert subscribed["type"] == "subscribed"
    assert subscribed["run_id"] == fake.snapshots[0].run_id
    assert subscribed["latest_sequence"] == fake.snapshots[0].sequence
    assert fake.latest_sequence_reads == 0
    assert failure["type"] == "protocol_error"
    assert failure["code"] == "stream_failed"


def test_subscription_reports_structured_stream_failure(
    tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_replay(
        after_sequence: int, *, store_id: str | None = None, bootstrap_spine: bool = False
    ) -> Never:
        del after_sequence, store_id, bootstrap_spine
        _failure_message = "event store is unavailable"
        raise RuntimeError(_failure_message)

    def body(parts: ServerParts, dial: _Dial) -> list[dict[str, Any]]:
        client = dial()
        _send(
            client,
            SubscribeRequest(
                timestamp=REQUEST_TIME, after_sequence=0, client_id="failing-unix-client"
            ),
        )
        subscribed = _receive(client)
        bootstrap = _receive(client)
        parts.journal.record(EventType.CHAT, "hello", status="answered")
        failure = _receive(client)
        client.close()
        return [subscribed, bootstrap, failure]

    subscribed, bootstrap, failure = _served(
        socket_dir,
        tmp_path / "logs",
        body,
        prepare=lambda parts: monkeypatch.setattr(
            parts.api, "subscription_checkpoint", fail_replay
        ),
    )

    assert subscribed["type"] == "subscribed"
    assert bootstrap["type"] == "event_batch"
    assert failure["type"] == "protocol_error"
    assert failure["request_id"] == subscribed["request_id"]
    assert failure["client_id"] == "failing-unix-client"
    assert failure["code"] == "stream_failed"
    assert failure["diagnostic"]["detail"] == "RuntimeError: event store is unavailable"


def test_socket_path_limit_matches_kernel(socket_dir: Path) -> None:
    name = "a" * (MAX_SOCKET_PATH_BYTES - len(str(socket_dir)) - 1)
    longest = socket_dir / name

    assert validate_socket_path(longest) is longest
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as accepted:
        accepted.settimeout(HANG_GUARD_S)
        accepted.bind(str(longest))
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as rejected:
        rejected.settimeout(HANG_GUARD_S)
        with pytest.raises(OSError, match="too long"):
            rejected.bind(f"{longest}a")


def test_transport_rejects_overlong_path_before_binding(tmp_path: Path) -> None:
    path = tmp_path / ("d" * MAX_SOCKET_PATH_BYTES) / "server.sock"
    parts = build_server_parts()

    try:
        with pytest.raises(SocketPathTooLongError) as failure:
            UnixJsonlServer(
                path,
                parts.api,
                subscriptions=None,
                threads=OsThreads(),
                network=UnixNetwork(),
                key=CONTROL_KEY,
            ).start()

        assert failure.value.path == path
        assert str(MAX_SOCKET_PATH_BYTES) in str(failure.value)
        assert not path.parent.exists()
    finally:
        parts.close()
