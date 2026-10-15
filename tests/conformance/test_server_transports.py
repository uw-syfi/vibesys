"""Execute shared conformance scenarios against the real server transports."""

from __future__ import annotations

import json
import socket
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, cast

import pytest
from pydantic import TypeAdapter
from tests.conformance.frame_matching import assert_frame_matches
from tests.conformance.replay import ScenarioConnection, load_scenario, run_steps, runner_group
from tests.server.support import (
    DEADLOCK_GUARD_S,
    FakeSettleWindow,
    ServerParts,
    build_server_parts,
)
from websockets.sync.client import ClientConnection, connect

from server.api.protocol import ActiveExecutions
from server.transport.subscriptions import SubscriptionTracker
from server.transport.unix_jsonl import UnixJsonlServer
from server.transport.websocket import WebSocketGateway, WebSocketLimits
from vs_sim.api import OsThreads, ThreadBlockingRunner, UnixNetwork
from vs_sim.api.testing import ManualClock

if TYPE_CHECKING:
    from collections.abc import Generator, Iterable, Mapping
    from pathlib import Path

    from websockets.typing import Origin

_HISTORY_EVENTS = 100
# A burst large enough that one batch per event would be unmistakable, split
# into chunks so the coalescing bound is a ratio rather than a timing guess.
_BURST_CHUNKS = 10
_BURST_CHUNK_EVENTS = 10
_BURST_EVENTS = _BURST_CHUNKS * _BURST_CHUNK_EVENTS


class _UnixConnection:
    def __init__(self, path: Path) -> None:
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.settimeout(DEADLOCK_GUARD_S)
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
        payload = self._connection.recv(timeout=DEADLOCK_GUARD_S)
        assert isinstance(payload, str), "WebSocket transport returned a binary frame"
        return cast("dict[str, Any]", json.loads(payload))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()


def _parts_with_history(tmp_path: Path) -> ServerParts:
    parts = build_server_parts(tmp_path / "logs")
    for index in range(_HISTORY_EVENTS):
        parts.journal.publish_output("stdout", f"conformance-{index}")
    return parts


def _declared_runs(scenario_names: Iterable[str]) -> list[tuple[str, str]]:
    """Pair each scenario with every transport it declares it must reproduce."""
    return [
        (name, transport)
        for name in scenario_names
        for transport in load_scenario(name)["transports"]
    ]


@contextmanager
def _running_connection(
    transport: str,
    parts: ServerParts,
    socket_path: Path,
    limits: WebSocketLimits | None = None,
) -> Generator[ScenarioConnection]:
    if transport == "unix":
        with UnixJsonlServer(
            socket_path, parts.api, subscriptions=None, threads=OsThreads(), network=UnixNetwork()
        ):
            connection = _UnixConnection(socket_path)
            try:
                yield connection
            finally:
                connection.close()
        return
    assert transport == "websocket", f"unknown transport: {transport}"
    with WebSocketGateway(
        parts.api,
        limits=limits or WebSocketLimits(),
        clock=ManualClock(),
        threads=OsThreads(),
        request_runner=ThreadBlockingRunner(),
    ) as gateway:
        connection = _WebSocketConnection(gateway)
        try:
            yield connection
        finally:
            connection.close()


@pytest.mark.parametrize("scenario_name", runner_group("server", "bootstrap"))
@pytest.mark.parametrize("transport", ["unix", "websocket"])
def test_shared_bootstrap_scenarios_run_against_each_transport(
    tmp_path: Path,
    socket_dir: Path,
    scenario_name: str,
    transport: str,
) -> None:
    scenario = load_scenario(scenario_name)
    assert transport in scenario["transports"]
    parts = _parts_with_history(tmp_path)
    try:
        with _running_connection(transport, parts, socket_dir / "conformance.sock") as connection:
            received = run_steps(connection, scenario)
        batch = received[-1]
        if scenario_name == "tail-bootstrap-spine-prepend":
            assert batch["history_after_sequence"] == parts.api.latest_sequence - 50
        else:
            assert batch["history_after_sequence"] == 0
    finally:
        parts.close()


@pytest.mark.parametrize("scenario_name", runner_group("server", "active-checkpoint"))
@pytest.mark.parametrize("transport", ["unix", "websocket"])
def test_shared_active_checkpoint_scenario_runs_against_each_transport(
    tmp_path: Path,
    socket_dir: Path,
    scenario_name: str,
    transport: str,
) -> None:
    """Both transports reconstruct the same oversized authoritative list."""
    scenario = load_scenario(scenario_name)
    assert transport in scenario["transports"]
    parts = build_server_parts(tmp_path / "logs")
    assignment = "x" * 1_100_000
    execution = parts.start_execution("implementer", "round-1", assignment)
    try:
        with _running_connection(
            transport, parts, socket_dir / "active-checkpoint.sock"
        ) as connection:
            received = run_steps(connection, scenario)
            event_batch, first_chunk = received[-2:]
            chunks = [first_chunk]
            while len(chunks) < first_chunk["chunk_count"]:
                chunks.append(connection.receive())

        checkpoint_id = first_chunk["checkpoint_id"]
        assert event_batch["active_executions_checkpoint_id"] == checkpoint_id
        assert [chunk["chunk_index"] for chunk in chunks] == list(range(len(chunks)))
        assert {chunk["chunk_count"] for chunk in chunks} == {len(chunks)}
        assert {chunk["checkpoint_id"] for chunk in chunks} == {checkpoint_id}
        active = TypeAdapter(ActiveExecutions).validate_json(
            "".join(chunk["payload"] for chunk in chunks)
        )
        assert [(item.execution_id, item.assignment) for item in active] == [
            (execution.execution_id, assignment)
        ]
    finally:
        parts.close()


@pytest.mark.parametrize("tail", [None, 20])
@pytest.mark.parametrize("transport", ["unix", "websocket"])
def test_future_resume_cursor_replays_the_live_store_on_each_transport(
    tmp_path: Path,
    socket_dir: Path,
    tail: int | None,
    transport: str,
) -> None:
    parts = _parts_with_history(tmp_path)
    latest = parts.api.latest_sequence
    request = {
        "type": "subscribe",
        "timestamp": "2026-01-01T00:00:00Z",
        "after_sequence": latest + 100,
        **({} if tail is None else {"tail": tail}),
    }
    try:
        with _running_connection(transport, parts, socket_dir / "future-cursor.sock") as connection:
            connection.send(request)
            assert connection.receive()["type"] == "subscribed"
            batch = connection.receive()

        expected_floor = 0 if tail is None else max(0, latest - tail)
        assert batch["history_after_sequence"] == expected_floor
        assert batch["through_sequence"] == latest
        assert [
            event["sequence"] for event in batch["events"] if event["sequence"] > expected_floor
        ] == list(range(expected_floor + 1, latest + 1))
    finally:
        parts.close()


@pytest.mark.parametrize("scenario_name", runner_group("server", "tail-overflow"))
@pytest.mark.parametrize("transport", ["unix", "websocket"])
def test_tail_overflow_rebootstrap_scenario_marks_only_the_second_bootstrap(
    tmp_path: Path,
    socket_dir: Path,
    scenario_name: str,
    transport: str,
) -> None:
    """Exercise the corpus's dynamic tail-overflow setup against both servers."""
    scenario = load_scenario(scenario_name)
    assert transport in scenario["transports"]
    parts = build_server_parts(tmp_path / "logs")
    try:
        with _running_connection(transport, parts, socket_dir / "tail-overflow.sock") as connection:
            steps = scenario["steps"]
            connection.send(steps[0]["frame"])
            assert_frame_matches(connection.receive(), steps[1]["expect"])
            assert_frame_matches(connection.receive(), steps[2]["expect"])

            # Publish one complete over-tail burst while holding the condition
            # shared with the stream loop. It cannot observe a prefix and send
            # an ordinary continuation before it sees the overflow.
            with parts.condition:
                for index in range(11):
                    parts.journal.publish_output("stdout", f"overflow-{index}")

            assert_frame_matches(connection.receive(), steps[3]["expect"])
    finally:
        parts.close()


def test_a_rebootstrap_split_across_frames_supersedes_the_fold_once(
    tmp_path: Path,
    socket_dir: Path,
) -> None:
    """Only the first frame of a chunked rebootstrap carries the marker.

    ``WP-REBOOTSTRAP`` defines the marker as "supersedes the fold". The gateway
    splits a batch that exceeds its send cap into several ``event_batch``
    frames, and a client following the definition discards its fold on every
    marked frame, so a marker on each frame would leave it holding only the
    last chunk of the replay. The lowered send cap and the long event texts
    make the tail of ten events span several frames.
    """
    parts = build_server_parts(tmp_path / "logs")
    limits = WebSocketLimits(send_frame_bytes=2048)
    try:
        with _running_connection(
            "websocket", parts, socket_dir / "split-rebootstrap.sock", limits
        ) as connection:
            connection.send(
                {
                    "type": "subscribe",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "after_sequence": 0,
                    "tail": 10,
                }
            )
            assert connection.receive()["type"] == "subscribed"
            assert connection.receive()["type"] == "event_batch"
            with parts.condition:
                for index in range(11):
                    parts.journal.publish_output("stdout", f"overflow-{index}-" + "x" * 400)
            frames = [connection.receive()]
            while frames[-1]["through_sequence"] < parts.api.latest_sequence:
                frames.append(connection.receive())
    finally:
        parts.close()

    assert len(frames) > 1
    assert [frame["rebootstrap"] for frame in frames] == [True] + [False] * (len(frames) - 1)
    sequences = [event["sequence"] for frame in frames for event in frame["events"]]
    assert sequences == sorted(set(sequences))
    assert sequences[-10:] == list(
        range(parts.api.latest_sequence - 9, parts.api.latest_sequence + 1)
    )


def _acknowledges_the_command(reply: Mapping[str, Any]) -> None:
    """One response carries the ack for the command it answers."""
    assert reply["ack"]["action"] == "pause"


def _carries_a_chat_result(reply: Mapping[str, Any]) -> None:
    """The dedicated chat connection's single response carries the answer."""
    assert reply["chat"] is not None


# Response scenarios exercised against every transport they declare. The
# scenario steps carry the shared assertions; callbacks add checks that are
# specific to server-side state or behavior outside the wire contract.
_CONTROL_PATH_CALLBACKS = {
    "command-ack-roundtrip": _acknowledges_the_command,
    "chat-dedicated-connection": _carries_a_chat_result,
}


@pytest.mark.parametrize(
    ("scenario_name", "transport"),
    _declared_runs(runner_group("server", "control")),
)
def test_control_path_scenarios_run_against_each_declared_transport(
    tmp_path: Path,
    socket_dir: Path,
    scenario_name: str,
    transport: str,
) -> None:
    """Scenarios whose reply is a ``Response``, which carries no ``type``.

    These were structurally valid and unexecuted until #1040: the runner
    compared the corpus pseudo-type against a field the response envelope
    deliberately does not have. The declared step assertions, plus any
    server-side callback registered above, keep the relaxation honest so a
    response that merely parses is not mistaken for the right response.
    """
    parts = build_server_parts(tmp_path / "logs")
    try:
        with _running_connection(transport, parts, socket_dir / "control.sock") as connection:
            received = run_steps(connection, load_scenario(scenario_name))
        assert len(received) == 1
        callback = _CONTROL_PATH_CALLBACKS.get(scenario_name)
        if callback is not None:
            callback(received[0])
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
            connection.send(
                {"type": "subscribe", "timestamp": "2026-01-01T00:00:00Z", "after_sequence": 0}
            )
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


def _disconnect_waiter(tracker: SubscriptionTracker) -> tuple[threading.Thread, threading.Event]:
    """A not-yet-started thread that waits for no stream, and the event it sets on return."""
    settled = threading.Event()

    def wait_for_disconnect() -> None:
        tracker.wait_for_none_active(settle_seconds=0.1)
        settled.set()

    return threading.Thread(target=wait_for_disconnect, daemon=True), settled


@pytest.mark.parametrize("scenario_name", runner_group("server", "dual-client"))
def test_dual_transport_subscriptions_have_independent_floors_and_teardown(
    tmp_path: Path,
    socket_dir: Path,
    scenario_name: str,
) -> None:
    scenario = load_scenario(scenario_name)
    parts = _parts_with_history(tmp_path)
    settle = FakeSettleWindow()
    tracker = SubscriptionTracker(settle, threads=OsThreads())
    socket_path = socket_dir / "dual.sock"
    waiter, settled = _disconnect_waiter(tracker)

    try:
        with (
            UnixJsonlServer(
                socket_path, parts.api, tracker, threads=OsThreads(), network=UnixNetwork()
            ),
            WebSocketGateway(
                parts.api,
                subscriptions=tracker,
                clock=ManualClock(),
                threads=OsThreads(),
                request_runner=ThreadBlockingRunner(),
            ) as gateway,
        ):
            connections: dict[str, ScenarioConnection] = {
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
                        assert_frame_matches(message, step["expect"])
                        received[step["client"]].append(message)

                latest = parts.api.latest_sequence
                terminal_batch = received["terminal"][-1]
                browser_batch = received["browser"][-1]
                assert terminal_batch["history_after_sequence"] == latest - 60
                assert browser_batch["history_after_sequence"] == latest - 20
                assert terminal_batch["through_sequence"] == latest
                assert browser_batch["through_sequence"] == latest

                waiter.start()
                connections["terminal"].close()
                parts.journal.publish_output("stdout", "browser remains live")
                live = connections["browser"].receive()
                assert live["type"] == "event_batch"
                assert live["through_sequence"] == latest + 1
                # The browser stream is still active, so the waiter has no
                # stream-free moment to settle on and has opened no window.
                assert not settled.is_set()
                assert settle.windows_opened == 0

                connections["browser"].close()
                settle.await_window(1)
                settle.elapse()
                assert settled.wait(timeout=DEADLOCK_GUARD_S)
            finally:
                for connection in connections.values():
                    connection.close()
        assert not socket_path.exists()
    finally:
        parts.close()
        if waiter.is_alive():
            waiter.join(timeout=DEADLOCK_GUARD_S)
        assert not waiter.is_alive()
