"""Detachable lifetime and read-only reopen contracts."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from typing import TYPE_CHECKING

import pytest
from tests.server.support import DEADLOCK_GUARD_S, build_server_parts

from launch import default_runs
from server.api.protocol import SnapshotQuery, StopCommand, SubscribeRequest
from server.events import EventType
from server.runtime import ServerRuntime
from server.transport.discovery import WebInstanceHold, WebInstanceRecord
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vs_sim.api.testing import stop_process

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


def test_detached_runtime_runs_without_a_subscriber_and_exits_when_the_run_ends(
    tmp_path: Path,
) -> None:
    """A headless detached server serves clients mid-run and ends with its run.

    Nothing it serves outlives the run (there is no web gateway to keep a page
    open), so it exits without a ``shutdown`` request; a finished run is
    reopened read-only from stored state instead.
    """
    socket_path = tmp_path / "control.sock"
    milestones: list[str] = []

    class Observer:
        def listening(self) -> None:
            milestones.append("listening")

        def run_ready(self, run_id: str) -> None:
            milestones.append(run_id)

    runtime = ServerRuntime(
        runs=default_runs(), socket_path=socket_path, detach=True, observer=Observer()
    )
    started = threading.Event()
    finish = runtime.threads.event()
    holder: dict[str, object] = {}

    def run() -> str:
        started.set()
        assert finish.wait(timeout=DEADLOCK_GUARD_S)
        return "done"

    thread = threading.Thread(target=lambda: holder.setdefault("result", runtime.run(run)))
    thread.start()
    # A detached run's callback starts only once the transport accepts clients.
    assert started.wait(timeout=DEADLOCK_GUARD_S)
    assert runtime.transport_listening.is_set()
    assert milestones == ["listening"]

    # A client that attaches mid-run is served through the reconnect bootstrap.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(DEADLOCK_GUARD_S)
        client.connect(str(socket_path))
        with client.makefile("rwb") as stream:
            stream.write(SubscribeRequest(after_sequence=0).model_dump_json().encode() + b"\n")
            stream.flush()
            assert json.loads(stream.readline())["type"] == "subscribed"

    finish.set()
    thread.join(timeout=DEADLOCK_GUARD_S)
    assert not thread.is_alive(), "a headless detached server must exit when its run ends"
    assert holder["result"] == "done"
    assert not socket_path.exists()


@pytest.mark.parametrize(
    ("failure_factory", "terminal_type"),
    [
        pytest.param(
            lambda: RuntimeError("callback failed"),
            EventType.RUN_FAILED,
            id="generic",
        ),
        pytest.param(
            lambda: ConfigurationError(
                ConfigurationDiagnostic(
                    code="invalid_arguments",
                    stage="argument_parsing",
                    message="invalid argument",
                    usage="usage: vibesys",
                )
            ),
            EventType.CONFIGURATION_FAILED,
            id="configuration",
        ),
    ],
)
def test_detached_runtime_unwinds_callback_failures_without_shutdown(
    tmp_path: Path,
    failure_factory: Callable[[], ConfigurationError | RuntimeError],
    terminal_type: EventType,
) -> None:
    """A callback failure records its terminal event and releases its transport.

    The wait is a deadlock guard, not a scheduling verdict: the callback or
    runtime has no external work to await, and the test always releases a
    merge-base runtime from its old detached wait in ``finally``.
    """
    runtime = ServerRuntime(
        runs=default_runs(),
        socket_path=tmp_path / "control.sock",
        detach=True,
    )
    failure = failure_factory()
    completed = threading.Event()
    raised: list[ConfigurationError | RuntimeError] = []

    def fail() -> None:
        raise failure

    def invoke() -> None:
        try:
            runtime.run(fail)
        except (ConfigurationError, RuntimeError) as error:
            raised.append(error)
        finally:
            completed.set()

    thread = threading.Thread(target=invoke)
    thread.start()
    try:
        assert completed.wait(timeout=DEADLOCK_GUARD_S), (
            "callback failure must unwind without shutdown"
        )
    finally:
        runtime.shutdown()
        thread.join(timeout=DEADLOCK_GUARD_S)

    assert not thread.is_alive()
    assert raised == [failure]
    assert [event.type for event in runtime.journal.read() if event.type is terminal_type] == [
        terminal_type
    ]
    assert not runtime.socket_path.exists()


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


def test_relocated_journal_replays_its_recorded_identity(tmp_path: Path) -> None:
    original = tmp_path / "original" / "logs"
    writer = build_server_parts(original)
    writer.controller.finish()
    recorded_run_id = writer.journal.run_id_locked()
    writer.close()
    relocated = tmp_path / "relocated-parent" / "logs"
    relocated.parent.mkdir()
    original.rename(relocated)

    reader = build_server_parts()
    reader.controller.attach_read_only(relocated)

    snapshot = reader.api.execute(SnapshotQuery())
    bootstrap = reader.api.subscription_bootstrap(0, None)
    assert snapshot.snapshot is not None
    assert snapshot.snapshot.run_id == recorded_run_id
    assert bootstrap.run_id == recorded_run_id
    assert {event.run_id for event in bootstrap.events} == {recorded_run_id}
    reader.close()


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
        stop_process(child)

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
            stop_process(child)


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
        stop_process(reader)

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
