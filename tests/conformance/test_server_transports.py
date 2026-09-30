"""Execute shared conformance scenarios against the real server transports."""

from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

import pytest
from tests.server.support import ServerParts, build_server_parts
from websockets.sync.client import ClientConnection, connect

from server.transport.subscriptions import SubscriptionTracker
from server.transport.unix_jsonl import UnixJsonlServer
from server.transport.websocket import WebSocketGateway

if TYPE_CHECKING:
    from collections.abc import Generator, Mapping

    from websockets.typing import Origin

_SCENARIOS = Path(__file__).parent / "scenarios"
_HISTORY_EVENTS = 100
# A burst large enough that one batch per event would be unmistakable, split
# into chunks so the coalescing bound is a ratio rather than a timing guess.
_BURST_CHUNKS = 10
_BURST_CHUNK_EVENTS = 10
_BURST_EVENTS = _BURST_CHUNKS * _BURST_CHUNK_EVENTS


class _Connection(Protocol):
    def send(self, frame: Mapping[str, Any]) -> None: ...

    def receive(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


class _UnixConnection:
    def __init__(self, path: Path) -> None:
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(2)
        self._socket.connect(str(path))
        self._stream = self._socket.makefile("rwb")
        self._closed = False

    def send(self, frame: Mapping[str, Any]) -> None:
        self._stream.write(json.dumps(frame).encode() + b"\n")
        self._stream.flush()

    def receive(self) -> dict[str, Any]:
        payload = self._stream.readline()
        assert payload, "Unix transport closed before the expected frame"
        return cast("dict[str, Any]", json.loads(payload))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream.close()
        self._socket.close()


class _WebSocketConnection:
    def __init__(self, gateway: WebSocketGateway) -> None:
        origin = f"http://127.0.0.1:{gateway.bound_port}"
        self._connection: ClientConnection = connect(
            gateway.websocket_url,
            origin=cast("Origin", origin),
            open_timeout=2,
            close_timeout=2,
        )
        self._closed = False

    def send(self, frame: Mapping[str, Any]) -> None:
        self._connection.send(json.dumps(frame))

    def receive(self) -> dict[str, Any]:
        payload = self._connection.recv(timeout=2)
        assert isinstance(payload, str), "WebSocket transport returned a binary frame"
        return cast("dict[str, Any]", json.loads(payload))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()


def _scenario(name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", json.loads((_SCENARIOS / f"{name}.json").read_text()))


def _parts_with_history(tmp_path: Path) -> ServerParts:
    parts = build_server_parts(tmp_path / "logs")
    for index in range(_HISTORY_EVENTS):
        parts.journal.publish_output("stdout", f"conformance-{index}")
    return parts


def _assert_subset(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    for key, value in expected.items():
        assert actual.get(key) == value, f"expected {key}={value!r}, received {actual.get(key)!r}"


def _run_steps(
    connection: _Connection,
    scenario: Mapping[str, Any],
) -> list[dict[str, Any]]:
    received: list[dict[str, Any]] = []
    for step in scenario["steps"]:
        if step["dir"] == "c2s":
            connection.send(step["frame"])
        else:
            message = connection.receive()
            _assert_subset(message, step["expect"])
            received.append(message)
    return received


@contextmanager
def _running_connection(
    transport: str,
    parts: ServerParts,
    socket_path: Path,
) -> Generator[_Connection]:
    if transport == "unix":
        with UnixJsonlServer(socket_path, parts.api):
            connection = _UnixConnection(socket_path)
            try:
                yield connection
            finally:
                connection.close()
        return
    assert transport == "websocket", f"unknown transport: {transport}"
    with WebSocketGateway(parts.api) as gateway:
        connection = _WebSocketConnection(gateway)
        try:
            yield connection
        finally:
            connection.close()


@pytest.mark.parametrize("scenario_name", ["full-replay-bootstrap", "tail-bootstrap-spine-prepend"])
@pytest.mark.parametrize("transport", ["unix", "websocket"])
def test_shared_bootstrap_scenarios_run_against_each_transport(
    tmp_path: Path,
    socket_dir: Path,
    scenario_name: str,
    transport: str,
) -> None:
    scenario = _scenario(scenario_name)
    assert transport in scenario["transports"]
    parts = _parts_with_history(tmp_path)
    try:
        with _running_connection(transport, parts, socket_dir / "conformance.sock") as connection:
            received = _run_steps(connection, scenario)
        batch = received[-1]
        if scenario_name == "tail-bootstrap-spine-prepend":
            assert batch["history_after_sequence"] == parts.api.latest_sequence - 50
        else:
            assert batch["history_after_sequence"] == 0
    finally:
        parts.close()


@pytest.mark.parametrize("transport", ["unix", "websocket"])
def test_a_backlog_published_between_checkpoints_arrives_as_coalesced_batches(
    tmp_path: Path,
    socket_dir: Path,
    transport: str,
) -> None:
    """Checkpoint coalescing is a property of both transports, not a WebSocket accident.

    ``subscription_checkpoint`` returns everything past the cursor, so however
    many events land between two checkpoints arrive as one batch. That is what
    keeps a consumer that reads less often than the journal is written cheap,
    instead of repainting once per event.

    Each chunk is published under the shared condition the stream loop waits
    on, so a chunk can never be observed half-written. The consumer's cursor
    therefore always sits on a chunk boundary and every batch carries at least
    one whole chunk, which makes the batch count bounded by construction
    rather than by how fast the host happens to be.

    Frame size, on the other hand, is bounded only by ``_BURST_EVENTS``: a
    batch is 364 bytes per event plus a 144 to 155 byte envelope, so the whole
    burst in one frame is about 36.6 kB. Measured over 210 runs, 45 cold
    processes and 60 warm iterations per transport, batch counts ranged over
    {1, 2, 3, 4} and the largest frame was 36548B, one cold WebSocket run that
    coalesced all 100 events. That is above ``send_buffer_bytes`` (32 KiB) and
    harmless: ``write_limit`` reaches only ``transport.set_write_buffer_limits``,
    and ``transport.write`` appends a whole frame to an unbounded buffer before
    consulting the mark, so overrunning it suspends the following ``drain()``
    rather than failing or truncating the send. Confirmed directly: a forced
    single batch of 1000 events delivers intact as one 366052B frame.

    What this pins is therefore the coalescing, on both transports, and not any
    stall. The consumer here reads continuously. That the WebSocket send path
    really does stall rather than buffer without bound is a separate claim,
    asserted in ``tests/server/test_websocket_transport.py`` by the write
    deadline firing, which can only happen if a write made no progress.
    """
    parts = build_server_parts(tmp_path / "logs")
    try:
        with _running_connection(transport, parts, socket_dir / "burst.sock") as connection:
            connection.send({"type": "subscribe", "after_sequence": 0})
            assert connection.receive()["type"] == "subscribed"
            assert connection.receive()["type"] == "event_batch"

            for chunk in range(_BURST_CHUNKS):
                with parts.condition:
                    for index in range(_BURST_CHUNK_EVENTS):
                        parts.journal.publish_output("stdout", f"burst-{chunk}-{index}")

            latest = parts.api.latest_sequence
            batches: list[dict[str, Any]] = []
            while not batches or batches[-1]["through_sequence"] < latest:
                batch = connection.receive()
                assert batch["type"] == "event_batch"
                batches.append(batch)

            delivered = [event for batch in batches for event in batch["events"]]
            assert len(delivered) == _BURST_EVENTS
            assert len(batches) <= _BURST_CHUNKS
    finally:
        parts.close()


def test_dual_transport_subscriptions_have_independent_floors_and_teardown(
    tmp_path: Path,
    socket_dir: Path,
) -> None:
    scenario = _scenario("dual-transport-independent-subscriptions")
    parts = _parts_with_history(tmp_path)
    tracker = SubscriptionTracker()
    socket_path = socket_dir / "dual.sock"
    settled = threading.Event()

    def wait_for_disconnect() -> None:
        tracker.wait_for_none_active(settle_seconds=0.1)
        settled.set()

    waiter = threading.Thread(
        target=wait_for_disconnect,
        daemon=True,
    )

    try:
        with (
            UnixJsonlServer(socket_path, parts.api, tracker),
            WebSocketGateway(parts.api, subscriptions=tracker) as gateway,
        ):
            connections: dict[str, _Connection] = {
                "terminal": _UnixConnection(socket_path),
                "browser": _WebSocketConnection(gateway),
            }
            received: dict[str, list[dict[str, Any]]] = {name: [] for name in connections}
            try:
                for step in scenario["steps"]:
                    connection = connections[step["client"]]
                    if step["dir"] == "c2s":
                        connection.send(step["frame"])
                    else:
                        message = connection.receive()
                        _assert_subset(message, step["expect"])
                        received[step["client"]].append(message)

                latest = parts.api.latest_sequence
                terminal_batch = received["terminal"][-1]
                browser_batch = received["browser"][-1]
                assert terminal_batch["history_after_sequence"] == latest - 60
                assert browser_batch["history_after_sequence"] == latest - 20
                assert terminal_batch["through_sequence"] == latest
                assert browser_batch["through_sequence"] == latest

                waiter.start()
                assert not settled.wait(timeout=0.1)
                connections["terminal"].close()
                parts.journal.publish_output("stdout", "browser remains live")
                live = connections["browser"].receive()
                assert live["type"] == "event_batch"
                assert live["through_sequence"] == latest + 1
                assert not settled.wait(timeout=0.2)

                connections["browser"].close()
                assert settled.wait(timeout=2)
            finally:
                for connection in connections.values():
                    connection.close()
        assert not socket_path.exists()
    finally:
        parts.close()
        if waiter.is_alive():
            waiter.join(timeout=2)
        assert not waiter.is_alive()
