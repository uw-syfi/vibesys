"""Real Unix-transport coverage for iteration store-generation fencing."""

from __future__ import annotations

import json
import socket
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from tests.server.support import CONTROL_KEY, REQUEST_TIME, build_server_parts

from server.api.protocol import IterationQuery, SubscribeRequest
from server.events import EventType, RunEvent, RunStartedData
from server.transport.unix_jsonl import UnixJsonlServer
from vs_sim.api import OsThreads, UnixNetwork
from vs_sim.api.testing import HANG_GUARD_S

if TYPE_CHECKING:
    from collections.abc import Callable, Generator
    from pathlib import Path


def _write_log(directory: Path) -> Path:
    directory.mkdir(parents=True)
    started = RunEvent(
        sequence=1,
        run_id="replacement-run",
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        type=EventType.RUN_STARTED,
        data=RunStartedData(outer_loop="single-agent", input="objective"),
    )
    (directory / "run-events.jsonl").write_text(started.model_dump_json() + "\n")
    return directory


@contextmanager
def _subscribed(
    socket_path: Path,
) -> Generator[Callable[[], dict[str, object]]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(HANG_GUARD_S)
        client.connect(str(socket_path))
        client.sendall(CONTROL_KEY.hello())
        with client.makefile("rwb") as stream:
            stream.write(
                SubscribeRequest(timestamp=REQUEST_TIME, after_sequence=0)
                .model_dump_json()
                .encode()
                + b"\n"
            )
            stream.flush()
            yield lambda: json.loads(stream.readline())


def _request(socket_path: Path, request: IterationQuery) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(HANG_GUARD_S)
        client.connect(str(socket_path))
        client.sendall(CONTROL_KEY.hello())
        with client.makefile("rwb") as stream:
            stream.write(request.model_dump_json().encode() + b"\n")
            stream.flush()
            return json.loads(stream.readline())


def test_iteration_query_follows_the_subscribed_store_across_control_sockets(
    tmp_path: Path,
) -> None:
    parts = build_server_parts(tmp_path / "server")
    socket_path = tmp_path / "server.sock"

    with (
        UnixJsonlServer(
            socket_path,
            parts.api,
            subscriptions=None,
            threads=OsThreads(),
            network=UnixNetwork(),
            key=CONTROL_KEY,
        ),
        _subscribed(socket_path) as read,
    ):
        assert read()["type"] == "subscribed"
        bootstrap = read()
        original_store = bootstrap["store_id"]
        assert isinstance(original_store, str)
        assert (
            _request(
                socket_path,
                IterationQuery(timestamp=REQUEST_TIME, expected_store_id=original_store),
            )["ok"]
            is True
        )
        assert read()["store_id"] == original_store

        parts.attach(_write_log(tmp_path / "logs"))
        replacement = read()
        replacement_store = replacement["store_id"]
        assert isinstance(replacement_store, str)
        assert replacement_store != original_store

        stale = _request(
            socket_path, IterationQuery(timestamp=REQUEST_TIME, expected_store_id=original_store)
        )
        current = _request(
            socket_path, IterationQuery(timestamp=REQUEST_TIME, expected_store_id=replacement_store)
        )

    assert stale["ok"] is False
    diagnostic = stale["diagnostic"]
    assert isinstance(diagnostic, dict)
    assert diagnostic["code"] == "iteration_store_replaced"
    assert current["ok"] is True
