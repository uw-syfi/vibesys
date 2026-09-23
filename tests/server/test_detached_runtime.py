"""Detachable lifetime and read-only reopen contracts."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest
from tests.server.support import build_server_parts

from server.api.protocol import SnapshotQuery, StopCommand, SubscribeRequest
from server.runtime import ServerRuntime
from server.transport.discovery import WebInstanceRecord

if TYPE_CHECKING:
    from pathlib import Path


def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        # test-isolation: poll the filesystem for the real transport endpoint
        time.sleep(0.01)
    assert path.exists()


def test_detached_runtime_runs_without_a_subscriber_and_accepts_reattach(
    tmp_path: Path,
) -> None:
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path, detach=True)
    completed = threading.Event()
    holder: dict[str, object] = {}

    def run() -> str:
        completed.set()
        return "done"

    thread = threading.Thread(target=lambda: holder.setdefault("result", runtime.run(run)))
    thread.start()
    _wait_for(socket_path)
    assert completed.wait(timeout=2)
    assert thread.is_alive(), "detached runtime must outlive its run callback"

    # A late subscriber receives the already-recorded terminal event through
    # the same bootstrap path used by reconnecting clients.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(str(socket_path))
        with client.makefile("rwb") as stream:
            stream.write(SubscribeRequest(after_sequence=0).model_dump_json().encode() + b"\n")
            stream.flush()
            messages: list[dict[str, Any]] = []
            for _ in range(4):
                message = json.loads(stream.readline())
                messages.append(message)
                if any(event["type"] == "run_finished" for event in message.get("events", [])):
                    break
    assert any(
        event["type"] == "run_finished"
        for message in messages
        for event in message.get("events", [])
    )

    runtime.shutdown()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert holder["result"] == "done"
    assert not socket_path.exists()


def test_finished_journal_reopens_read_only_without_mutating_storage(tmp_path: Path) -> None:
    log_dir = tmp_path / "finished-run"
    writer = build_server_parts(log_dir)
    writer.controller.finish()
    writer.close()
    before = {path: path.read_bytes() for path in log_dir.iterdir() if path.is_file()}

    reader = build_server_parts()
    reader.controller.attach_read_only(log_dir)
    snapshot = reader.api.execute(SnapshotQuery())
    assert snapshot.ok is True
    assert snapshot.snapshot is not None
    assert snapshot.snapshot.status.value == "completed"

    # Queries may still use the normal API, but neither query bookkeeping nor
    # a command can mutate the durable journal.
    history = reader.api.execute(SnapshotQuery())
    assert history.ok is True
    with pytest.raises(RuntimeError) as failure:
        reader.api.execute(StopCommand())
    assert getattr(failure.value, "diagnostic_code", None) == "run_read_only"
    reader.close()
    assert before == {path: path.read_bytes() for path in before}


def test_stale_instance_record_is_removed(tmp_path: Path) -> None:
    path = tmp_path / "web-gateway.json"
    record = WebInstanceRecord(
        pid=999_999_999,
        port=43_211,
        token="stale",  # noqa: S106  # lint-waiver: LW-101059 [S106]; use a deliberately recognizable token in a stale-record fixture
        url="http://127.0.0.1:43211/?token=stale",
        project_root=str(tmp_path),
        started_at=0.0,
    )
    record.write(path)

    assert WebInstanceRecord.discover(path) is None
    assert not path.exists()
