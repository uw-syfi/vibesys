"""Composition and terminal-event tests for the interactive server runtime."""

import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from server.api.service import RunApi
from server.chat.manager import ChatManager
from server.controller import RunController
from server.execution import ExecutionTracker
from server.integration import RunIntegrationAdapter
from server.journal import EventJournal
from server.runtime import ServerRuntime
from server.wire import codec, messages
from server.wire.v2 import common_pb2, events_pb2, server_messages_pb2
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEventType, EventStatus

RUN_FINISHED = events_pb2.EventType.EVENT_TYPE_RUN_FINISHED


def _await_socket(socket_path: Path) -> None:
    deadline = time.monotonic() + 5
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


@contextmanager
def _subscription(socket_path: Path) -> Generator[Callable[[], server_messages_pb2.ServerMessage]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(socket_path))
        with client.makefile("rwb") as stream:
            request = messages.make_request("subscribe", after_sequence=0)
            stream.write(codec.dumps(request).encode() + b"\n")
            stream.flush()
            yield lambda: codec.loads(server_messages_pb2.ServerMessage, stream.readline())


def _batch(message: server_messages_pb2.ServerMessage) -> list[events_pb2.RunEvent]:
    return list(message.event_batch.events)


def _collect_until(
    socket_path: Path,
    terminal_type: events_pb2.EventType.ValueType,
    received: list[events_pb2.RunEvent],
) -> None:
    _await_socket(socket_path)
    with _subscription(socket_path) as read:
        while True:
            events = _batch(read())
            received.extend(events)
            if any(event.type == terminal_type for event in events):
                return


def test_runtime_explicitly_composes_server_components(tmp_path):  # noqa: ANN001, ANN201
    runtime = ServerRuntime(socket_path=tmp_path / "control.sock")

    assert isinstance(runtime.journal, EventJournal)
    assert isinstance(runtime.executions, ExecutionTracker)
    assert isinstance(runtime.controller, RunController)
    assert isinstance(runtime.chat, ChatManager)
    assert isinstance(runtime.integration, RunIntegrationAdapter)
    assert isinstance(runtime.api, RunApi)
    assert runtime.chat.terminal_retention_enabled()

    runtime.integration.close()


def test_runtime_streams_success_before_client_disconnect(tmp_path):  # noqa: ANN001, ANN201
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path)
    received: list[events_pb2.RunEvent] = []
    subscriber = threading.Thread(
        target=_collect_until,
        args=(socket_path, RUN_FINISHED, received),
    )
    subscriber.start()

    value = runtime.run(lambda: "ran")

    subscriber.join(timeout=5)
    assert value == "ran"
    assert not subscriber.is_alive()
    assert any(event.type == events_pb2.EventType.EVENT_TYPE_SERVER_READY for event in received)
    assert sum(event.type == RUN_FINISHED for event in received) == 1
    assert not socket_path.exists()


def test_runtime_waits_for_reconnected_subscriber_before_teardown(tmp_path: Path) -> None:
    """A subscriber that reconnects must outlive the run's terminal event.

    The first subscription drops before the run finishes; the runtime must not
    treat that as "the client is gone" while the second subscription is live.
    """
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path)
    release_run = threading.Event()
    run_returned = threading.Event()
    returned_while_attached: list[bool] = []

    def drive_reconnect() -> None:
        _await_socket(socket_path)
        with _subscription(socket_path) as read:
            assert read().WhichOneof("body") == "subscribed"
        with _subscription(socket_path) as read:
            assert read().WhichOneof("body") == "subscribed"
            release_run.set()
            while not any(event.type == RUN_FINISHED for event in _batch(read())):
                pass
            returned_while_attached.append(run_returned.wait(timeout=1.0))

    clients = threading.Thread(target=drive_reconnect)
    clients.start()

    def run() -> str:
        assert release_run.wait(timeout=5)
        return "ran"

    value = runtime.run(run)

    run_returned.set()
    clients.join(timeout=5)
    assert value == "ran"
    assert not clients.is_alive()
    assert returned_while_attached == [False]
    assert not socket_path.exists()


def test_runtime_returns_cleanly_after_an_operator_stop(tmp_path):  # noqa: ANN001, ANN201
    """An in-band `/stop` ends the backend without a failure record.

    The journal's terminal record is the `stopped` status change; no
    `run_finished`, `run_failed`, or `run_interrupted` event follows it.
    """
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path)
    received: list[events_pb2.RunEvent] = []

    def collect_until_stopped() -> None:
        _await_socket(socket_path)
        with _subscription(socket_path) as read:
            while True:
                events = _batch(read())
                received.extend(events)
                if any(
                    event.type == events_pb2.EventType.EVENT_TYPE_RUN_STATUS_CHANGED
                    and event.run_status_changed.status == common_pb2.RunStatus.RUN_STATUS_STOPPED
                    for event in events
                ):
                    return

    subscriber = threading.Thread(target=collect_until_stopped)
    subscriber.start()

    def run() -> str:
        runtime.api.execute(messages.make_request("stop"))
        runtime.controller.before_agent("implementer", "round 1", "work")
        return "unreachable"

    value = runtime.run(run)

    subscriber.join(timeout=5)
    assert value is None
    assert not subscriber.is_alive()
    terminal = (
        events_pb2.EventType.EVENT_TYPE_RUN_FINISHED,
        events_pb2.EventType.EVENT_TYPE_RUN_FAILED,
        events_pb2.EventType.EVENT_TYPE_RUN_INTERRUPTED,
    )
    assert not any(event.type in terminal for event in received)
    statuses = [
        event.run_status_changed.status
        for event in received
        if event.type == events_pb2.EventType.EVENT_TYPE_RUN_STATUS_CHANGED
    ]
    assert statuses[-2:] == [
        common_pb2.RunStatus.RUN_STATUS_STOPPING,
        common_pb2.RunStatus.RUN_STATUS_STOPPED,
    ]
    assert not socket_path.exists()


def test_runtime_does_not_duplicate_core_terminal_event(tmp_path):  # noqa: ANN001, ANN201
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path)
    received: list[events_pb2.RunEvent] = []
    subscriber = threading.Thread(
        target=_collect_until,
        args=(socket_path, RUN_FINISHED, received),
    )
    subscriber.start()

    def run() -> None:
        runtime.integration.events.emit(
            CoreEventType.RUN_FINISHED,
            status=EventStatus.COMPLETED,
        )

    runtime.run(run)

    subscriber.join(timeout=5)
    assert not subscriber.is_alive()
    assert sum(event.type == RUN_FINISHED for event in received) == 1
    assert sum(event.type == RUN_FINISHED for event in runtime.journal.read()) == 1


def test_runtime_streams_configuration_failure_without_run_failure(tmp_path):  # noqa: ANN001, ANN201
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path)
    received: list[events_pb2.RunEvent] = []
    subscriber = threading.Thread(
        target=_collect_until,
        args=(socket_path, events_pb2.EventType.EVENT_TYPE_CONFIGURATION_FAILED, received),
    )
    subscriber.start()
    failure = ConfigurationError(
        ConfigurationDiagnostic(
            code="invalid_arguments",
            stage="argument_parsing",
            message="unknown token=super-secret option --bad",
            usage="usage: vibesys --token=super-secret",
        )
    )

    with pytest.raises(ConfigurationError) as raised:
        runtime.run(lambda: (_ for _ in ()).throw(failure))

    assert raised.value is failure
    subscriber.join(timeout=5)
    assert not subscriber.is_alive()
    event = next(
        event
        for event in received
        if event.type == events_pb2.EventType.EVENT_TYPE_CONFIGURATION_FAILED
    )
    assert event.configuration_failed.code == "invalid_arguments"
    assert event.configuration_failed.message == "unknown token=[REDACTED] option --bad"
    assert event.configuration_failed.usage == "usage: vibesys --token=[REDACTED]"
    assert not any(event.type == events_pb2.EventType.EVENT_TYPE_RUN_FAILED for event in received)
