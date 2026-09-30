"""Detachable lifetime and read-only reopen contracts."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, Any

import pytest
from tests.server.support import build_server_parts

from server.api.protocol import SnapshotQuery, StopCommand, SubscribeRequest
from server.runtime import ServerRuntime
from server.transport.discovery import WebInstanceHold, WebInstanceRecord

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


def test_an_idle_instance_directory_reads_free(tmp_path: Path) -> None:
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    instance_path.parent.mkdir(parents=True)
    log_path = WebInstanceHold.log_path(instance_path)
    assert log_path == instance_path.parent / "web-gateway.json.log"

    hold = WebInstanceHold.observe(instance_path)

    # Asking whether the instance is in use must not create the file it reads,
    # and the caller's own process is never one of the holders it is told about.
    assert hold == WebInstanceHold(holders=(), log_locked=False)
    assert hold.free is True
    assert not log_path.exists()


def test_the_hold_outlives_the_record_and_the_launcher(tmp_path: Path) -> None:
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    instance_path.parent.mkdir(parents=True)
    log_path = WebInstanceHold.log_path(instance_path)

    descriptor = os.open(log_path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(descriptor, "w+b") as output:
        assert WebInstanceHold.take(descriptor) is True
        rival = os.open(log_path, os.O_RDWR)
        try:
            # Exclusion is what lets the launcher refuse a second gateway.
            assert WebInstanceHold.take(rival) is False
        finally:
            os.close(rival)
        child = subprocess.Popen(
            [sys.executable, "-c", "import signal; signal.pause()"],
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
        )
    try:
        _record_for(child.pid).write(instance_path)
        _record_for(child.pid).remove_if_owner(instance_path)

        # The launcher has closed its own descriptor and the record has come and
        # gone, which is the state a stopping gateway leaves behind. The child
        # inherited the descriptor, so lock and log are still one open file
        # description and both observations still name the child: this is the
        # inherited-descriptor property the whole postcondition rests on.
        assert not instance_path.exists()
        hold = WebInstanceHold.observe(instance_path)
        assert hold == WebInstanceHold(holders=(child.pid,), log_locked=True)
        assert hold.free is False
    finally:
        child.terminate()
        child.wait()

    assert WebInstanceHold.observe(instance_path).free is True


def test_a_process_using_the_directory_without_the_lock_is_still_reported(
    tmp_path: Path,
) -> None:
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    instance_path.parent.mkdir(parents=True)

    # Two shapes the lock alone cannot see: a gateway launched without
    # `--detach`, which writes no startup log and only holds its claim lock,
    # and a startup log opened by a launcher that predates the lock. Treating
    # an unlocked log as an idle directory is what let `stop` report success
    # over a live gateway.
    for path in (instance_path.with_name("web-gateway.json.lock"), instance_path):
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "w+b") as output:
            child = subprocess.Popen(
                [sys.executable, "-c", "import signal; signal.pause()"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        try:
            hold = WebInstanceHold.observe(instance_path)
            assert hold.holders == (child.pid,)
            assert hold.log_locked is False
            assert hold.free is False
        finally:
            child.terminate()
            child.wait()


_SHARED_LOCK_HOLDER = """
import fcntl, os, signal, sys
descriptor = os.open(sys.argv[1], os.O_RDONLY)
fcntl.flock(descriptor, fcntl.LOCK_SH)
os.write(1, b"1")
signal.pause()
"""


def test_observing_the_hold_does_not_make_the_observer_a_holder(tmp_path: Path) -> None:
    instance_path = tmp_path / ".vibesys" / "web-gateway.json"
    instance_path.parent.mkdir(parents=True)
    log_path = WebInstanceHold.log_path(instance_path)
    log_path.touch(mode=0o600)
    reader = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-994221 [S603]; the command is this interpreter running a literal in-test program, and its one argument is a pytest tmp_path
        [sys.executable, "-c", _SHARED_LOCK_HOLDER, str(log_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
    )
    assert reader.stdout is not None
    assert reader.stdout.read(1) == b"1"
    try:
        # An observation answers by taking a *shared* lock, which conflicts with
        # the launcher's exclusive one but not with another observation. Answering
        # with an exclusive lock made the observer a holder for the duration, so
        # two callers asking at once each reported an idle directory as in use,
        # and a launch racing an observation aborted for no reason. A process
        # that holds only a shared lock is therefore not a lock holder here.
        hold = WebInstanceHold.observe(instance_path)
        assert hold.log_locked is False
        assert hold.holders == (reader.pid,)
    finally:
        reader.terminate()
        reader.wait()

    descriptor = os.open(log_path, os.O_RDWR)
    try:
        assert WebInstanceHold.take(descriptor) is True
    finally:
        os.close(descriptor)


def _record_for(pid: int) -> WebInstanceRecord:
    token = "held" + "-token"
    return WebInstanceRecord(
        pid=pid,
        port=8765,
        token=token,
        url=f"http://127.0.0.1:8765/?token={token}",
        project_root="/project",
        started_at=1.0,
    )
