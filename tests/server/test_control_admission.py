"""A run's control socket serves only a client that presents the operator key first.

Each test drives a real ``UnixJsonlServer`` over ``SimNetwork`` and ``SimThreads``, so
admission is checked without a real socket, clock or confinement. A refused client reads
exactly one ``control_key_refused`` protocol error and then the end of the stream: no
request it sent was answered, so none was served.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.support import CONTROL_KEY, build_server_parts

from server.api.protocol import ProtocolErrorMessage, Response
from server.transport.unix_jsonl import (
    HELLO_TIMEOUT_SECONDS,
    MAX_UNADMITTED_SESSIONS,
    UnixJsonlServer,
)
from vs_project.api import CONTROL_HELLO_MAX_BYTES, OperatorKey
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from vs_sim.api import Connection

_STEER = (
    json.dumps(
        {
            "type": "command.steer",
            "request_id": "req-steer",
            "timestamp": "2026-01-01T00:00:00Z",
            "text": "stop the run",
        }
    ).encode()
    + b"\n"
)
_SNAPSHOT = (
    json.dumps(
        {
            "type": "query.snapshot",
            "request_id": "req-snapshot",
            "timestamp": "2026-01-01T00:00:00Z",
        }
    ).encode()
    + b"\n"
)


def _until_end(connection: Connection) -> list[bytes]:
    received = b""
    while chunk := connection.recv(4096, HANG_GUARD_S):
        received += chunk
    return received.splitlines(keepends=True)


def _exchange(socket_path: Path, writes: list[bytes], *, seed: int = 0) -> list[bytes]:
    """Send ``writes`` one by one to a fresh server and read its reply lines."""
    threads = SimThreads(schedule_seed=seed)

    def main() -> list[bytes]:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        with UnixJsonlServer(socket_path, parts.api, None, threads, network, key=CONTROL_KEY):
            client = network.connect(str(socket_path), HANG_GUARD_S)
            for write in writes:
                client.send(write, HANG_GUARD_S)
            lines = _first_lines(client, writes)
            client.close()
            return lines

    return threads.run(main)


def _first_lines(client: Connection, writes: list[bytes]) -> list[bytes]:
    """Read one reply per request line after the first, or up to the end of the stream."""
    expected = max(b"".join(writes).count(b"\n") - 1, 1)
    received = b""
    while received.count(b"\n") < expected:
        chunk = client.recv(4096, HANG_GUARD_S)
        if not chunk:
            break
        received += chunk
    return received.splitlines(keepends=True)


def _assert_refused(lines: list[bytes]) -> None:
    assert len(lines) == 1, lines
    refusal = ProtocolErrorMessage.model_validate_json(lines[0])
    assert refusal.code == "control_key_refused"
    # The operator's usual mistake is a client and server with different runtime roots.
    assert "XDG_RUNTIME_DIR" in refusal.model_dump_json()


@settings(max_examples=60, deadline=None)
@given(first=st.binary(max_size=CONTROL_HELLO_MAX_BYTES - 1).filter(lambda b: b"\n" not in b))
def test_any_first_line_but_the_hello_is_refused_and_nothing_is_served(
    tmp_path_factory: pytest.TempPathFactory, first: bytes
) -> None:
    socket_path = tmp_path_factory.mktemp("sock") / "control.sock"

    lines = _exchange(socket_path, [first + b"\n" + _STEER + _SNAPSHOT])

    _assert_refused(lines)


@settings(max_examples=20, deadline=None)
@given(secret=st.text(alphabet="0123456789abcdef", min_size=64, max_size=64))
def test_another_keys_hello_is_refused(
    tmp_path_factory: pytest.TempPathFactory, secret: str
) -> None:
    other = OperatorKey(secret)
    socket_path = tmp_path_factory.mktemp("sock") / "control.sock"

    lines = _exchange(socket_path, [other.hello() + _STEER])

    if other.admits(CONTROL_KEY.hello()):
        assert Response.model_validate_json(lines[0]).ok
    else:
        _assert_refused(lines)


@settings(max_examples=40, deadline=None)
@given(data=st.data(), seed=st.integers(0, 2**16))
def test_the_hello_is_admitted_however_it_is_split_or_joined_to_the_first_request(
    tmp_path_factory: pytest.TempPathFactory, data: st.DataObject, seed: int
) -> None:
    stream = CONTROL_KEY.hello() + _SNAPSHOT
    cuts = sorted(data.draw(st.sets(st.integers(1, len(stream) - 1), max_size=6)))
    writes = [
        stream[start:end] for start, end in zip([0, *cuts], [*cuts, len(stream)], strict=True)
    ]
    socket_path = tmp_path_factory.mktemp("sock") / "control.sock"

    lines = _exchange(socket_path, writes, seed=seed)

    assert len(lines) == 1, lines
    assert Response.model_validate_json(lines[0]).ok


def test_a_first_line_longer_than_any_hello_is_refused(tmp_path: Path) -> None:
    lines = _exchange(tmp_path / "control.sock", [b"x" * (2 * CONTROL_HELLO_MAX_BYTES), _STEER])

    _assert_refused(lines)


def test_the_hello_followed_by_a_tail_without_newline_within_the_bound_is_refused(
    tmp_path: Path,
) -> None:
    """The hello's own bytes, unterminated and padded past the bound, are not a hello."""
    padded = CONTROL_KEY.hello().rstrip(b"\n") + b" " * CONTROL_HELLO_MAX_BYTES + b"\n"

    lines = _exchange(tmp_path / "control.sock", [padded, _STEER])

    _assert_refused(lines)


def test_a_client_that_stays_silent_is_refused_after_the_hello_timeout(tmp_path: Path) -> None:
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[list[bytes], float]:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        with UnixJsonlServer(
            tmp_path / "control.sock", parts.api, None, threads, network, key=CONTROL_KEY
        ):
            client = network.connect(str(tmp_path / "control.sock"), HANG_GUARD_S)
            started = threads.now()
            lines = _until_end(client)
            waited = threads.now() - started
            client.close()
            return lines, waited

    lines, waited = threads.run(main)

    _assert_refused(lines)
    assert waited >= HELLO_TIMEOUT_SECONDS


@settings(max_examples=10, deadline=None)
@given(pause=st.floats(HELLO_TIMEOUT_SECONDS / 8, HELLO_TIMEOUT_SECONDS * 0.9))
def test_a_hello_trickled_byte_by_byte_is_refused_at_one_overall_deadline(
    tmp_path_factory: pytest.TempPathFactory, pause: float
) -> None:
    """Each byte arrives within the timeout of the last; the whole hello does not."""
    socket_path = tmp_path_factory.mktemp("sock") / "control.sock"
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[list[bytes], float]:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        with UnixJsonlServer(socket_path, parts.api, None, threads, network, key=CONTROL_KEY):
            client = network.connect(str(socket_path), HANG_GUARD_S)
            started = threads.now()
            hello = CONTROL_KEY.hello()
            for index in range(len(hello)):
                if client.peer_closed():
                    break
                threads.sleep(pause)
                try:
                    client.send(hello[index : index + 1], HANG_GUARD_S)
                except OSError:
                    break
            lines = _until_end(client)
            waited = threads.now() - started
            client.close()
            return lines, waited

    lines, waited = threads.run(main)

    _assert_refused(lines)
    # One deadline for the whole line: refused within a pause of it, not after a timeout
    # per byte (the old bound was about len(hello) * HELLO_TIMEOUT_SECONDS).
    assert waited <= HELLO_TIMEOUT_SECONDS + pause + 1


def test_a_connection_over_the_unadmitted_cap_evicts_the_longest_waiting_one(
    tmp_path: Path,
) -> None:
    """Silent peers fill every slot; the next connection is served and the oldest is closed."""
    socket_path = tmp_path / "control.sock"
    threads = SimThreads(schedule_seed=0)

    def main() -> tuple[list[bytes], float, list[bool], list[bytes]]:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        with UnixJsonlServer(socket_path, parts.api, None, threads, network, key=CONTROL_KEY):
            silent = []
            for _ in range(MAX_UNADMITTED_SESSIONS):
                silent.append(network.connect(str(socket_path), HANG_GUARD_S))
                # Accepted one by one, so the order of waiting is the order of connecting.
                threads.sleep(HELLO_TIMEOUT_SECONDS / 100)
            started = threads.now()
            operator = network.connect(str(socket_path), HANG_GUARD_S)
            operator.send(CONTROL_KEY.hello() + _SNAPSHOT, HANG_GUARD_S)
            served = _first_lines(operator, [CONTROL_KEY.hello() + _SNAPSHOT])
            oldest = _until_end(silent[0])
            evicted_after = threads.now() - started
            still_open = [not peer.peer_closed() for peer in silent[1:]]
            operator.close()
            for peer in silent:
                peer.close()
            return oldest, evicted_after, still_open, served

    oldest, evicted_after, still_open, served = threads.run(main)

    assert Response.model_validate_json(served[0]).ok
    # Closed unread, without waiting out its hello timeout.
    assert oldest == []
    assert evicted_after < HELLO_TIMEOUT_SECONDS
    assert all(still_open)


@settings(max_examples=10, deadline=None)
@given(seed=st.integers(0, 2**16))
def test_peers_that_reconnect_at_once_cannot_lock_the_operator_out(
    tmp_path_factory: pytest.TempPathFactory, seed: int
) -> None:
    """Peers without the key keep every slot full; every operator connection is still served.

    Each peer reconnects as soon as it is refused or evicted, the worst a peer can do to the
    slots. Before eviction, the new connection was the one closed, and none was served.
    """
    socket_path = tmp_path_factory.mktemp("sock") / "control.sock"
    threads = SimThreads(schedule_seed=seed)

    def main() -> tuple[int, int]:
        parts = build_server_parts(None, threads=threads)
        network = SimNetwork(threads)
        with UnixJsonlServer(socket_path, parts.api, None, threads, network, key=CONTROL_KEY):
            stop = threads.event()

            def occupy() -> None:
                while not stop.is_set():
                    peer = network.connect(str(socket_path), HANG_GUARD_S)
                    with suppress(OSError):
                        while peer.recv(4096, HANG_GUARD_S):
                            pass
                    peer.close()

            occupiers = [
                threads.spawn(occupy, name=f"occupier-{index}", daemon=True)
                for index in range(MAX_UNADMITTED_SESSIONS)
            ]
            threads.sleep(HELLO_TIMEOUT_SECONDS / 20)
            served = attempts = 0
            end = threads.now() + 3 * HELLO_TIMEOUT_SECONDS
            while threads.now() < end:
                attempts += 1
                operator = network.connect(str(socket_path), HANG_GUARD_S)
                try:
                    operator.send(CONTROL_KEY.hello() + _SNAPSHOT, HANG_GUARD_S)
                    lines = _first_lines(operator, [CONTROL_KEY.hello() + _SNAPSHOT])
                    served += bool(lines) and Response.model_validate_json(lines[0]).ok
                except (OSError, ValueError):
                    pass
                operator.close()
                threads.sleep(HELLO_TIMEOUT_SECONDS / 4)
            stop.set()
            for occupier in occupiers:
                occupier.join(HANG_GUARD_S)
            return attempts, served

    attempts, served = threads.run(main)

    assert attempts > 1
    assert served == attempts
