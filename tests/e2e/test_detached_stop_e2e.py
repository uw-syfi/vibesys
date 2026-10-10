"""Real-socket proof that ``instances stop`` ends a detached run over its control socket.

A detached server is composed in process around a run that ends only when its
session is told to stop, registered in a real registry, and stopped through
``LiveRegistry.stop`` with the production socket requester. The signaller
refuses to signal, so a stop that did not go through the socket would show as
``unsupported`` rather than killing the test process.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from launch import default_runs
from server.api.protocol import SubscribeRequest
from server.instances import (
    ControlSocketStopRequester,
    FileInstanceStore,
    InstanceStatus,
    LiveInstanceRecord,
    LiveRegistry,
    StopEffects,
    StopOutcome,
    StopRoute,
    host_facts,
    instance_root,
    instance_socket_path,
    new_instance_id,
)
from server.runtime import ServerRuntime
from vibesys.run.integration import LocalRunIntegration
from vs_sim.api import OsThreads, UnixNetwork
from vs_sim.api.testing import HANG_GUARD_S

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from vibesys.api import RunSession
    from vs_runtime.api.infrastructure import RunControlChannel


class _NoSignal:
    """A host that cannot signal: the only way to stop is the control socket."""

    def terminate_if_current(self, pid: int, current: Callable[[], bool]) -> bool:
        del pid, current
        raise NotImplementedError


class _Session:
    """The ``RunControl`` slice of a session, over the run's control channel."""

    def __init__(self, control: RunControlChannel) -> None:
        self._control = control

    def stop(self) -> None:
        self._control.request_stop()


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    # A short runtime directory under `/tmp`, as production's fallback root
    # uses: pytest's temporary paths and macOS's `$TMPDIR` can push a socket
    # path past the 103-byte Unix limit.
    runtime = Path(tempfile.mkdtemp(prefix="vs-", dir="/tmp"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    try:
        yield instance_root(os.environ, os.getuid())
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


class _Subscriber:
    """A client's event stream, read until the run's terminal status."""

    def __init__(self, socket_path: Path) -> None:
        self.connection = UnixNetwork().connect(str(socket_path), HANG_GUARD_S)
        self.seen: list[str] = []
        self.subscribed = threading.Event()
        self.stopped = threading.Event()
        self.connection.send(SubscribeRequest(after_sequence=0).model_dump_json().encode() + b"\n")
        self.thread = threading.Thread(target=self._read)
        self.thread.start()

    def _read(self) -> None:
        buffer = b""
        while True:
            try:
                chunk = self.connection.recv(65536, HANG_GUARD_S)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                message = json.loads(line)
                self.seen.append(message["type"])
                self.seen.extend(
                    event["data"]["status"]
                    for event in message.get("events", [])
                    if event["type"] == "run_status_changed"
                )
            if "subscribed" in self.seen:
                self.subscribed.set()
            if "stopped" in self.seen:
                # The terminal status: nothing the test asserts follows it.
                self.stopped.set()
                return

    def close(self) -> None:
        self.thread.join(HANG_GUARD_S)
        self.connection.close()


def test_stop_asks_the_server_which_ends_its_run_and_unregisters(root: Path) -> None:
    instance_id = new_instance_id()
    socket_path = instance_socket_path(root, instance_id)
    socket_path.parent.mkdir(parents=True, mode=0o700)
    registry = LiveRegistry(FileInstanceStore(root))
    runtime = ServerRuntime(runs=default_runs(), socket_path=socket_path, detach=True)
    integration = LocalRunIntegration()
    integration.events.subscribe(runtime.integration.project_event)
    threads = OsThreads()
    outcome: list[object] = []

    def run() -> None:
        with runtime.condition:
            # The runtime reads only the `RunControl` slice of its session
            # (through `RunApi`'s session provider), which `_Session` implements.
            runtime.session = cast("RunSession", _Session(integration.control))
        deadline = threads.now() + HANG_GUARD_S
        while threads.now() < deadline:
            # The run's next controlled boundary: a requested stop lands here.
            integration.control.raise_if_stopped()
            threads.sleep(0.05)
        message = "the stop never reached the run"
        raise AssertionError(message)

    def serve() -> None:
        hostname, version = host_facts()
        with registry.register(instance_id) as hold:
            hold.publish(
                LiveInstanceRecord(
                    id=instance_id,
                    status=InstanceStatus.SERVING,
                    socket_path=str(socket_path),
                    project_root=str(root),
                    pid=os.getpid(),
                    started_at=threads.now(),
                    hostname=hostname,
                    vibesys_version=version,
                )
            )
            outcome.append(runtime.run(run))

    server = threading.Thread(target=serve)
    server.start()
    subscriber: _Subscriber | None = None
    try:
        assert runtime.transport_listening.wait(HANG_GUARD_S)
        subscriber = _Subscriber(socket_path)
        assert subscriber.subscribed.wait(HANG_GUARD_S)

        result = registry.stop(
            instance_id,
            StopEffects(
                ControlSocketStopRequester(UnixNetwork()), _NoSignal(), threads, threads.sleep
            ),
        )

        assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.CONTROL_SOCKET)
        # The server drained its streams before it released the record, so the
        # terminal status is already on the client's socket.
        assert subscriber.stopped.wait(HANG_GUARD_S)
    finally:
        if subscriber is not None:
            subscriber.close()
        integration.control.request_stop()
        server.join(HANG_GUARD_S)
        integration.close()

    # RunStopped is absorbed as a clean exit, and the record is gone.
    assert outcome == [None]
    assert registry.list().instances == ()
    assert subscriber is not None
    assert subscriber.seen[0] == "subscribed"
    statuses = [entry for entry in subscriber.seen if entry not in {"subscribed", "event_batch"}]
    assert statuses[-2:] == ["stopping", "stopped"]
