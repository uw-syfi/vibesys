"""Composition and terminal-event tests for the interactive server runtime."""

import json
import socket
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from server.api.protocol import StopCommand, SubscribeRequest
from server.api.service import RunApi
from server.chat.manager import ChatManager
from server.controller import RunController
from server.execution import ExecutionTracker
from server.integration import RunIntegrationAdapter
from server.journal import EventJournal
from server.runtime import ServerRuntime
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEventType, EventStatus
from vibesys.run.event_journal import EventJournal as CoreEventJournal
from vibesys.run.integration import LocalRunIntegration
from vibesys.run.run_control import RunControlChannel

if TYPE_CHECKING:
    from vibesys.api import RunSession


class _SessionControlStub:
    """Adapt a `RunControlChannel` to the `vibesys.api.RunControl` shape.

    Mirrors what `vibesys.api.session._LocalRunSession`'s `steer`/`pause`/
    `resume`/`stop` methods do in production, so a bare `runtime.run(...)`
    callback (which never goes through `ServerRuntime.drive`) can still stand
    in as the live session `runtime.api`'s `session_provider` reads.
    """

    def __init__(self, control: RunControlChannel) -> None:
        self._control = control

    def steer(self, text: str) -> None:
        self._control.queue_steer(text)

    def pause(self) -> None:
        self._control.request_pause()

    def resume(self) -> None:
        self._control.resume()

    def stop(self) -> None:
        self._control.request_stop()


def _await_socket(socket_path: Path) -> None:
    deadline = time.monotonic() + 5
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)


@contextmanager
def _subscription(socket_path: Path) -> Generator[Callable[[], dict]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(socket_path))
        with client.makefile("rwb") as stream:
            stream.write(SubscribeRequest(after_sequence=0).model_dump_json().encode() + b"\n")
            stream.flush()
            yield lambda: json.loads(stream.readline())


def _collect_until(socket_path: Path, terminal_type: str, received: list[dict]) -> None:
    _await_socket(socket_path)
    with _subscription(socket_path) as read:
        while True:
            events = read().get("events", [])
            received.extend(events)
            if any(event["type"] == terminal_type for event in events):
                return


def test_runtime_explicitly_composes_server_components(tmp_path: Path) -> None:
    runtime = ServerRuntime(socket_path=tmp_path / "control.sock")

    assert isinstance(runtime.journal, EventJournal)
    assert isinstance(runtime.executions, ExecutionTracker)
    assert isinstance(runtime.controller, RunController)
    assert isinstance(runtime.chat, ChatManager)
    assert isinstance(runtime.integration, RunIntegrationAdapter)
    assert isinstance(runtime.api, RunApi)
    assert runtime.chat.terminal_retention_enabled()

    runtime.integration.close()


def test_runtime_streams_success_before_client_disconnect(tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path)
    received: list[dict] = []
    subscriber = threading.Thread(
        target=_collect_until,
        args=(socket_path, "run_finished", received),
    )
    subscriber.start()

    value = runtime.run(lambda: "ran")

    subscriber.join(timeout=5)
    assert value == "ran"
    assert not subscriber.is_alive()
    assert any(event["type"] == "server_ready" for event in received)
    assert sum(event["type"] == "run_finished" for event in received) == 1
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
            assert read()["type"] == "subscribed"
        with _subscription(socket_path) as read:
            assert read()["type"] == "subscribed"
            release_run.set()
            while not any(event["type"] == "run_finished" for event in read().get("events", [])):
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


def test_runtime_returns_cleanly_after_an_operator_stop(tmp_path: Path) -> None:
    """An in-band `/stop` ends the backend without a failure record.

    The journal's terminal record is the `stopped` status change; no
    `run_finished`, `run_failed`, or `run_interrupted` event follows it.
    """
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path)
    received: list[dict] = []

    def collect_until_stopped() -> None:
        _await_socket(socket_path)
        with _subscription(socket_path) as read:
            while True:
                events = read().get("events", [])
                received.extend(events)
                if any(
                    event["type"] == "run_status_changed" and event["data"]["status"] == "stopped"
                    for event in events
                ):
                    return

    subscriber = threading.Thread(target=collect_until_stopped)
    subscriber.start()

    integration = LocalRunIntegration()
    integration.events.subscribe(runtime.integration.project_event)

    def run() -> str:
        with runtime.condition:
            # `_SessionControlStub` implements only the `RunControl` slice of
            # `RunSession`, which is all this path exercises: `runtime.run`
            # (unlike `ServerRuntime.drive`) never calls the query/workspace/
            # agent-host methods on `runtime.session`, only `session_provider`
            # (itself typed `RunControl | None`, see `server.api.service
            # .RunApi`). The cast documents that narrower real contract
            # instead of widening `_SessionControlStub` to satisfy every
            # `RunSession` member.
            runtime.session = cast("RunSession", _SessionControlStub(integration.control))
        runtime.api.execute(StopCommand())
        integration.control.raise_if_stopped()
        return "unreachable"

    try:
        value = runtime.run(run)
    finally:
        integration.close()

    subscriber.join(timeout=5)
    assert value is None
    assert not subscriber.is_alive()
    terminal = ("run_finished", "run_failed", "run_interrupted")
    assert not any(event["type"] in terminal for event in received)
    statuses = [
        event["data"]["status"] for event in received if event["type"] == "run_status_changed"
    ]
    assert statuses[-2:] == ["stopping", "stopped"]
    assert not socket_path.exists()


def test_runtime_does_not_duplicate_core_terminal_event(tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path)
    received: list[dict] = []
    subscriber = threading.Thread(
        target=_collect_until,
        args=(socket_path, "run_finished", received),
    )
    subscriber.start()

    core_events = CoreEventJournal()
    core_events.subscribe(runtime.integration.project_event)

    def run() -> None:
        core_events.emit(
            CoreEventType.RUN_FINISHED,
            status=EventStatus.COMPLETED,
        )

    runtime.run(run)

    subscriber.join(timeout=5)
    assert not subscriber.is_alive()
    assert sum(event["type"] == "run_finished" for event in received) == 1
    assert sum(event.type.value == "run_finished" for event in runtime.journal.read()) == 1


def test_runtime_streams_configuration_failure_without_run_failure(tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(socket_path=socket_path)
    received: list[dict] = []
    subscriber = threading.Thread(
        target=_collect_until,
        args=(socket_path, "configuration_failed", received),
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
    event = next(event for event in received if event["type"] == "configuration_failed")
    assert event["data"]["code"] == "invalid_arguments"
    assert event["data"]["message"] == "unknown token=[REDACTED] option --bad"
    assert event["data"]["usage"] == "usage: vibesys --token=[REDACTED]"
    assert not any(event["type"] == "run_failed" for event in received)
