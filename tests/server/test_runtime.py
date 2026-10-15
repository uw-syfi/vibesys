"""Composition and terminal-event tests for the interactive server runtime."""

import errno
import hashlib
import io
import json
import os
import socket
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, redirect_stdout
from http.client import HTTPConnection
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from tests.server.support import REQUEST_TIME, sim_platform
from tests.support.real_git import NO_GITHUB, REAL_GIT
from websockets.sync.client import connect

from entrypoints.production import production_runs
from server.api.protocol import (
    PauseCommand,
    ResumeCommand,
    SnapshotQuery,
    SteerCommand,
    StopCommand,
    SubscribeRequest,
)
from server.api.service import RunApi, RunControlUnavailableError
from server.chat.manager import ChatManager
from server.controller import RunController
from server.execution import ExecutionTracker
from server.host_api import FailureReason, RunStatus, StartupFact
from server.integration import RunIntegrationAdapter
from server.journal import WireJournal
from server.runtime import ServerRuntime
from server.transport.discovery import WebInstanceClaim, WebInstanceRecord
from vibesys.run.integration import LocalRunIntegration
from vs_core.api import StopMode
from vs_runtime.api.infrastructure import RunControlChannel
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api import OsThreads, Platform
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from websockets.typing import Origin


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
    callback (which never goes through `ServerRuntime.drive`) can install the
    same control role that a live session provides.
    """

    def __init__(self, control: RunControlChannel) -> None:
        self._control = control
        self.stop_calls = 0

    def steer(self, text: str) -> None:
        self._control.queue_steer(text)

    def pause(self) -> None:
        self._control.request_pause()

    def resume(self) -> None:
        self._control.resume()

    def resume_with_fallback(self) -> None:
        self._control.resume_with_fallback()

    def stop(self) -> None:
        self.stop_calls += 1
        self._control.request_stop(StopMode.DRAIN)


def _await_socket(runtime: ServerRuntime) -> None:
    assert runtime.transport_listening.wait(timeout=_DEADLOCK_GUARD_SECONDS)


@contextmanager
def _subscription(socket_path: Path) -> Generator[Callable[[], dict]]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_DEADLOCK_GUARD_SECONDS)
        client.connect(str(socket_path))
        with client.makefile("rwb") as stream:
            stream.write(
                SubscribeRequest(timestamp=REQUEST_TIME, after_sequence=0)
                .model_dump_json()
                .encode()
                + b"\n"
            )
            stream.flush()
            yield lambda: json.loads(stream.readline())


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
        browser.send(SubscribeRequest(timestamp=REQUEST_TIME, after_sequence=0).model_dump_json())
        yield lambda: json.loads(browser.recv(timeout=_DEADLOCK_GUARD_SECONDS))


def _web_request(page_url: str, origin: str) -> dict[str, Any]:
    """Execute one protocol request over the gateway's WebSocket."""
    with connect(
        _socket_url(page_url),
        origin=cast("Origin", origin),
        open_timeout=_DEADLOCK_GUARD_SECONDS,
        close_timeout=_DEADLOCK_GUARD_SECONDS,
    ) as browser:
        browser.send(SnapshotQuery(timestamp=REQUEST_TIME).model_dump_json())
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
    runtime = ServerRuntime(
        run_id="run-1",
        runs=production_runs(),
        socket_path=tmp_path / "control.sock",
        platform=sim_platform(SimThreads()),
    )

    assert isinstance(runtime.journal, WireJournal)
    assert isinstance(runtime.executions, ExecutionTracker)
    assert isinstance(runtime.controller, RunController)
    assert isinstance(runtime.chat, ChatManager)
    assert isinstance(runtime.integration, RunIntegrationAdapter)
    assert isinstance(runtime.api, RunApi)
    assert runtime.chat.terminal_retention_enabled()

    runtime.integration.close()


def test_runtime_snapshots_namespace_client_state_by_project(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    runtime = ServerRuntime(
        run_id="shared",
        project_root=project_root,
        runs=production_runs(),
        socket_path=tmp_path / "control.sock",
        platform=sim_platform(SimThreads()),
    )

    response = runtime.api.execute(SnapshotQuery(timestamp=REQUEST_TIME))
    expected = hashlib.sha256(project_root.resolve().as_posix().encode()).hexdigest()
    assert response.snapshot is not None
    assert response.snapshot.project_scope == expected
    runtime.integration.close()


def test_stop_acknowledged_while_transport_starts_reaches_the_session(tmp_path: Path) -> None:
    """A stop accepted from ``listening`` survives until session installation."""
    integration = LocalRunIntegration(
        clock=ManualClock(),
        git=REAL_GIT,
        github=NO_GITHUB,
        threads=OsThreads(),
        system=idle_environment_system(),
    )
    control = _SessionControlStub(integration.control)

    class Observer:
        def resolved(self, *, project_root: Path, task: str) -> None:
            del project_root, task

        def listening(self) -> None:
            response = runtime.api.execute(StopCommand(timestamp=REQUEST_TIME))
            assert response.ack is not None
            assert (response.ack.action, response.ack.status) == ("stop", "pending")

        def observe(
            self,
            *,
            status: RunStatus | None = None,
            failure: FailureReason | None = None,
            startup: StartupFact | None = None,
        ) -> None:
            del status, failure, startup

    runtime = ServerRuntime(
        run_id="run-1",
        runs=production_runs(),
        socket_path=tmp_path / "control.sock",
        detach=True,
        observer=Observer(),
        platform=Platform.real(),
    )

    def run() -> bool:
        runtime.controls.install(control)
        try:
            return integration.control.stop_requested()
        finally:
            runtime.controls.release(control)

    try:
        stop_reached_session = runtime.run(run)
    finally:
        integration.close()

    assert stop_reached_session is True
    assert control.stop_calls == 1
    with pytest.raises(RunControlUnavailableError, match="after the run session ends"):
        runtime.api.execute(StopCommand(timestamp=REQUEST_TIME))


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(PauseCommand(timestamp=REQUEST_TIME), id="pause"),
        pytest.param(ResumeCommand(timestamp=REQUEST_TIME), id="resume"),
        pytest.param(
            ResumeCommand(timestamp=REQUEST_TIME, fallback=True), id="resume-with-fallback"
        ),
        pytest.param(SteerCommand(timestamp=REQUEST_TIME, text="inspect startup"), id="steer"),
    ],
)
def test_startup_rejects_commands_that_require_an_active_session(
    tmp_path: Path,
    command: PauseCommand | ResumeCommand | SteerCommand,
) -> None:
    """No mutating command is acknowledged unless its effect can be retained."""

    class Observer:
        failure: RunControlUnavailableError | None = None

        def resolved(self, *, project_root: Path, task: str) -> None:
            del project_root, task

        def listening(self) -> None:
            with pytest.raises(RunControlUnavailableError) as raised:
                runtime.api.execute(command)
            self.failure = raised.value

        def observe(
            self,
            *,
            status: RunStatus | None = None,
            failure: FailureReason | None = None,
            startup: StartupFact | None = None,
        ) -> None:
            del status, failure, startup

    observer = Observer()
    runtime = ServerRuntime(
        run_id="run-1",
        runs=production_runs(),
        socket_path=tmp_path / "control.sock",
        detach=True,
        observer=observer,
        platform=Platform.real(),
    )

    assert runtime.run(lambda: "ran") == "ran"
    assert observer.failure is not None
    assert observer.failure.diagnostic_code == "run_control_unavailable"


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
        run_id="run-1",
        runs=production_runs(),
        socket_path=socket_path,
        web=True,
        instance_path=instance_path,
        platform=Platform.real(),
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
        run_id="run-1",
        runs=production_runs(),
        socket_path=socket_path,
        web=True,
        web_assets=assets,
        web_build_id=f"sha256:{'2' * 64}",
        web_origins=(declared_origin,),
        instance_path=instance_path,
        detach=True,
        platform=Platform.real(),
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
    assert record.web_build_id == f"sha256:{'2' * 64}"
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
        run_id="run-1",
        runs=production_runs(),
        socket_path=socket_path,
        web_assets=tmp_path / "web-dist",
        web_origins=("http://vibesys.test:4173",),
        instance_path=instance_path,
        detach=True,
        platform=Platform.real(),
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
            run_id="run-1",
            runs=production_runs(),
            socket_path=socket_path,
            web=True,
            web_port=held.getsockname()[1],
            instance_path=instance_path,
            detach=True,
            platform=Platform.real(),
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
