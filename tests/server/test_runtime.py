"""Composition and terminal-event tests for the interactive server runtime."""

import errno
import io
import json
import os
import socket
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from http.client import HTTPConnection
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.sync.client import connect

from launch import default_runs
from server.api.protocol import SnapshotQuery, StopCommand, SubscribeRequest
from server.api.service import RunApi
from server.chat.manager import ChatManager
from server.controller import RunController
from server.execution import ExecutionTracker
from server.integration import RunIntegrationAdapter
from server.journal import WireJournal
from server.runtime import ServerRuntime
from server.transport.discovery import WebInstanceClaim, WebInstanceRecord
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEventType, EventStatus
from vibesys.run.event_journal import EventJournal as CoreEventJournal
from vibesys.run.integration import LocalRunIntegration
from vs_runtime.api.infrastructure import RunControlChannel

if TYPE_CHECKING:
    from websockets.typing import Origin

    from vibesys.api import RunSession

# The one line `ServerRuntime.run` prints for a web launch, and the only place
# the minted capability token leaves the process.
_WEB_URL_PREFIX = "VibeSys web UI: "
# The name the gateway gives its event-loop thread, which is how a test sees
# whether the composition root constructed a gateway at all.
_GATEWAY_THREAD_NAME = "vibesys-server-websocket"
_INDEX_HTML = "<!doctype html><title>composed bundle</title>"
# A deadlock guard, not a synchronization budget: every wait that uses it is
# released by a signal the composition itself produces (a printed line, a
# protocol frame, a thread returning), so lowering this value cannot flip a
# verdict and expiring it only produces a failure message. See #1053 for the
# distinction this repository draws.
_DEADLOCK_GUARD_SECONDS = 30.0


class _SessionControlStub:
    """Adapt a `RunControlChannel` to the `vibesys.api.RunControl` shape.

    Mirrors what `vibesys.api._session._LocalRunSession`'s `steer`/`pause`/
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


def _complete_lines(value: str, prefix: str) -> tuple[str, ...]:
    """Return the newline-terminated lines of ``value`` that start with ``prefix``."""
    return tuple(line for line in value.split("\n")[:-1] if line.startswith(prefix))


class _PrintedLines(io.StringIO):
    """A Fake stdout that records the runtime's own published lines.

    `ServerRuntime.run` publishes the web gateway's capability URL by printing
    it, so stdout is part of what the composition observably produces. It is
    also the only signal available to a client: the gateway writes its
    instance record, then reports itself ready, then `run` prints, so a line
    arriving here means the port is bound and the record is on disk. Waiting
    for the line is therefore a deterministic handoff and needs no polling.

    This is a `StringIO` (so `contextlib.redirect_stdout`, the stdlib's own
    redirection seam, accepts it) that notifies a condition on every write.
    No code under `src/` is replaced.
    """

    def __init__(self) -> None:
        """Start empty, with the condition line waiters block on."""
        super().__init__()
        self._changed = threading.Condition()

    def write(self, text: str) -> int:
        """Record ``text`` and wake every waiter."""
        with self._changed:
            written = super().write(text)
            self._changed.notify_all()
        return written

    def lines(self, prefix: str = "") -> tuple[str, ...]:
        """Return the complete lines printed so far that start with ``prefix``."""
        with self._changed:
            return _complete_lines(self.getvalue(), prefix)

    def await_one(self, prefix: str) -> str:
        """Block until exactly one printed line starts with ``prefix``."""
        with self._changed:
            arrived = self._changed.wait_for(
                lambda: bool(_complete_lines(self.getvalue(), prefix)),
                timeout=_DEADLOCK_GUARD_SECONDS,
            )
        matched = self.lines(prefix)
        if not arrived or len(matched) != 1:
            message = f"expected one printed line starting with {prefix!r}, saw {matched!r}"
            raise AssertionError(message)
        return matched[0]


def _live_gateway_threads() -> int:
    """Return how many gateway event-loop threads are alive right now."""
    return sum(thread.name == _GATEWAY_THREAD_NAME for thread in threading.enumerate())


def _fetch_page(url: str) -> tuple[int, bytes]:
    """Fetch the capability URL exactly as a browser opening it would."""
    target = urlsplit(url)
    connection = HTTPConnection(target.hostname or "", target.port, timeout=_DEADLOCK_GUARD_SECONDS)
    try:
        connection.request("GET", f"{target.path}?{target.query}")
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _socket_url(page_url: str) -> str:
    """Derive the gateway's WebSocket endpoint from its page URL.

    This is the derivation a served page performs: same authority, the `/ws`
    path, and the capability token the page URL carries.
    """
    target = urlsplit(page_url)
    return f"ws://{target.netloc}/ws?{target.query}"


@contextmanager
def _web_subscription(page_url: str, origin: str) -> Generator[Callable[[], dict]]:
    """Subscribe over the gateway's WebSocket, as a browser client does."""
    with connect(
        _socket_url(page_url),
        origin=cast("Origin", origin),
        open_timeout=_DEADLOCK_GUARD_SECONDS,
        close_timeout=_DEADLOCK_GUARD_SECONDS,
    ) as browser:
        browser.send(SubscribeRequest(after_sequence=0).model_dump_json())
        yield lambda: json.loads(browser.recv(timeout=_DEADLOCK_GUARD_SECONDS))


def _web_request(page_url: str, origin: str) -> dict[str, Any]:
    """Execute one protocol request over the gateway's WebSocket."""
    with connect(
        _socket_url(page_url),
        origin=cast("Origin", origin),
        open_timeout=_DEADLOCK_GUARD_SECONDS,
        close_timeout=_DEADLOCK_GUARD_SECONDS,
    ) as browser:
        browser.send(SnapshotQuery().model_dump_json())
        return cast("dict[str, Any]", json.loads(browser.recv(timeout=_DEADLOCK_GUARD_SECONDS)))


def _error_chain(error: BaseException) -> tuple[BaseException, ...]:
    """Return ``error`` and every exception it was raised from."""
    chain: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__
    return tuple(chain)


def test_runtime_explicitly_composes_server_components(tmp_path: Path) -> None:
    runtime = ServerRuntime(runs=default_runs(), socket_path=tmp_path / "control.sock")

    assert isinstance(runtime.journal, WireJournal)
    assert isinstance(runtime.executions, ExecutionTracker)
    assert isinstance(runtime.controller, RunController)
    assert isinstance(runtime.chat, ChatManager)
    assert isinstance(runtime.integration, RunIntegrationAdapter)
    assert isinstance(runtime.api, RunApi)
    assert runtime.chat.terminal_retention_enabled()

    runtime.integration.close()


def test_runtime_streams_success_before_client_disconnect(tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    runtime = ServerRuntime(runs=default_runs(), socket_path=socket_path)
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
    runtime = ServerRuntime(runs=default_runs(), socket_path=socket_path)
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
    runtime = ServerRuntime(runs=default_runs(), socket_path=socket_path)
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
    runtime = ServerRuntime(runs=default_runs(), socket_path=socket_path)
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
    runtime = ServerRuntime(runs=default_runs(), socket_path=socket_path)
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


def test_a_web_only_subscriber_decides_a_web_runs_whole_lifetime(tmp_path: Path) -> None:
    """The gateway and the Unix server share one `SubscriptionTracker`.

    Run lifetime is decided entirely through the Unix transport: `run` waits
    for a subscriber on it before starting the backend, and waits for the last
    subscription to end before tearing the transports down. A client connected
    only over the WebSocket satisfies both, and the only thing that makes that
    true is the composition root handing the gateway the same tracker it
    handed `UnixJsonlServer`. Give the gateway a tracker of its own and this
    run has no subscriber at all: it fails the wait and raises "Timed out
    waiting for a server client".

    Both halves of the property are asserted, because they are separate
    consequences of the same sharing. The backend starts (`run` returns the
    callback's value, and the browser receives the terminal event), and
    teardown waits (`run` returns only after the browser disconnects).
    """
    socket_path = tmp_path / "control.sock"
    instance_path = tmp_path / "state" / "web-gateway.json"
    runtime = ServerRuntime(
        runs=default_runs(), socket_path=socket_path, web=True, instance_path=instance_path
    )
    printed = _PrintedLines()
    received: list[dict] = []

    def browse() -> None:
        page_url = printed.await_one(_WEB_URL_PREFIX).removeprefix(_WEB_URL_PREFIX)
        origin = f"http://{urlsplit(page_url).netloc}"
        with _web_subscription(page_url, origin) as read:
            while not any(event["type"] == "run_finished" for event in received):
                received.extend(read().get("events", []))

    # The executor joins the browser on exit and `result` re-raises whatever
    # it raised, so a failure there cannot be lost in the thread.
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-browser") as browsers:
        browser = browsers.submit(browse)
        with redirect_stdout(printed):
            value = runtime.run(lambda: "ran")
    browser.result()

    assert value == "ran"
    assert any(event["type"] == "server_ready" for event in received)
    assert sum(event["type"] == "run_finished" for event in received) == 1
    # Teardown, after `run` has returned: the gateway released its instance
    # and joined its thread, and the Unix server unlinked its socket.
    assert not instance_path.exists()
    assert WebInstanceClaim.is_held(instance_path) is False
    assert _live_gateway_threads() == 0
    assert not socket_path.exists()


def test_the_web_gateway_receives_its_assets_origins_and_instance_path(tmp_path: Path) -> None:
    """Every configured gateway argument arrives unswapped and is observable.

    `assets_dir` and `instance_path` are both `Path | None`, so transposing
    them type-checks and would otherwise be silent: it is caught here by the
    page body coming from the bundle directory while the instance record
    appears under the record path and nowhere else. `allowed_origins` is
    caught by a WebSocket handshake from the declared origin succeeding, which
    a gateway that never received the origin would answer 403.

    `detach=True` is the mechanism, not the subject: it skips the subscriber
    wait, so the run callback becomes a point inside the live composition
    where the gateway can be inspected from the calling thread with no
    cross-thread handoff. The URL is already printed by then, so nothing here
    waits for anything.
    """
    assets = tmp_path / "web-dist"
    (assets / "assets").mkdir(parents=True)
    (assets / "index.html").write_text(_INDEX_HTML)
    socket_path = tmp_path / "control.sock"
    instance_path = tmp_path / "state" / "web-gateway.json"
    declared_origin = "http://vibesys.test:4173"
    runtime = ServerRuntime(
        runs=default_runs(),
        socket_path=socket_path,
        web=True,
        web_assets=assets,
        web_origins=(declared_origin,),
        instance_path=instance_path,
        detach=True,
    )
    printed = _PrintedLines()
    observed: dict[str, Any] = {}

    def inspect_composition() -> str:
        # Requested first, not last: a detached run's teardown waits on this
        # event, so anything raised below would otherwise block `run` forever
        # instead of failing the test.
        runtime.shutdown()
        page_url = printed.await_one(_WEB_URL_PREFIX).removeprefix(_WEB_URL_PREFIX)
        observed["page_url"] = page_url
        observed["record"] = WebInstanceRecord.read(instance_path)
        observed["page"] = _fetch_page(page_url)
        observed["snapshot"] = _web_request(page_url, declared_origin)
        observed["gateway_threads"] = _live_gateway_threads()
        return "ran"

    with redirect_stdout(printed):
        assert runtime.run(inspect_composition) == "ran"

    target = urlsplit(observed["page_url"])
    token = parse_qs(target.query)["token"][0]
    assert (target.scheme, target.hostname) == ("http", "127.0.0.1")
    assert target.port is not None
    assert target.port > 0
    # The runtime passes no token, so the gateway mints one; it reaches the
    # operator only through this line.
    assert token
    assert observed["page_url"] == f"http://127.0.0.1:{target.port}/?token={token}"
    record = observed["record"]
    assert record is not None
    assert (record.pid, record.port, record.token) == (os.getpid(), target.port, token)
    assert record.url == observed["page_url"]
    assert not (assets / instance_path.name).exists()
    assert observed["page"] == (200, _INDEX_HTML.encode())
    assert observed["snapshot"]["ok"] is True
    assert observed["gateway_threads"] == 1
    assert not instance_path.exists()
    assert WebInstanceClaim.is_held(instance_path) is False
    assert _live_gateway_threads() == 0
    assert not socket_path.exists()


def test_the_runtime_composes_no_web_gateway_unless_web_is_requested(tmp_path: Path) -> None:
    """Without `web=True` the gateway is never constructed, even when configured.

    `instance_path` is supplied and still untouched afterwards. The gateway
    creates that directory to take its startup claim and writes its record
    there once bound, and it binds only from its own event-loop thread, so an
    absent directory and no such thread together mean no gateway object, no
    claim, and no listening socket.
    """
    socket_path = tmp_path / "control.sock"
    instance_path = tmp_path / "state" / "web-gateway.json"
    runtime = ServerRuntime(
        runs=default_runs(),
        socket_path=socket_path,
        web_assets=tmp_path / "web-dist",
        web_origins=("http://vibesys.test:4173",),
        instance_path=instance_path,
        detach=True,
    )
    printed = _PrintedLines()
    observed: dict[str, Any] = {}

    def inspect_composition() -> str:
        runtime.shutdown()
        observed["gateway_threads"] = _live_gateway_threads()
        observed["instance_dir"] = instance_path.parent.exists()
        return "ran"

    with redirect_stdout(printed):
        assert runtime.run(inspect_composition) == "ran"

    assert observed["gateway_threads"] == 0
    assert observed["instance_dir"] is False
    assert printed.lines() == ()
    assert not instance_path.parent.exists()
    assert not socket_path.exists()


def test_the_web_gateway_binds_the_configured_port(tmp_path: Path) -> None:
    """`web_port` is the port the gateway binds, so a held port fails the launch.

    This is the one gateway argument with no positive in-process observation:
    the default `0` binds an ephemeral port, so a composition root that
    hardcoded `port=0` would be indistinguishable from one that forwards
    `self.web_port`. Holding the port makes the difference observable without
    reserving anything from the ephemeral range and without a race, because
    the listener below owns `127.0.0.1:port` for the whole test and
    `SO_REUSEADDR` does not admit a second bind while an active listener holds
    the address.

    The failing launch is also the one path that tears the composition down
    from inside `ExitStack` setup rather than after a run, so the control
    socket and the gateway's startup claim are asserted released there too.
    Only the bind reason is asserted, not the message: naming the port in the
    message is #1029.
    """
    socket_path = tmp_path / "control.sock"
    instance_path = tmp_path / "state" / "web-gateway.json"
    printed = _PrintedLines()

    with socket.create_server(("127.0.0.1", 0)) as held:
        runtime = ServerRuntime(
            runs=default_runs(),
            socket_path=socket_path,
            web=True,
            web_port=held.getsockname()[1],
            instance_path=instance_path,
            detach=True,
        )
        with redirect_stdout(printed), pytest.raises(RuntimeError) as raised:
            # The callback is unreachable while the launch fails, and requests
            # shutdown so that a composition which wrongly starts still ends.
            runtime.run(runtime.shutdown)

    assert any(
        isinstance(error, OSError) and error.errno == errno.EADDRINUSE
        for error in _error_chain(raised.value)
    )
    assert printed.lines() == ()
    assert not instance_path.exists()
    assert WebInstanceClaim.is_held(instance_path) is False
    assert not socket_path.exists()
