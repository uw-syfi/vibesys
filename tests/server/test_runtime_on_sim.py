"""A server's whole lifetime on the simulator: serve, end with the run, close.

``ServerRuntime`` runs on ``SimThreads`` and ``SimNetwork``, so a seed decides
every interleaving of the run's end with the subscriber's reads, and every
wait is virtual.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.support import REQUEST_TIME, sim_platform
from tests.support.real_git import NO_GITHUB, REAL_GIT

from entrypoints.production import production_runs
from server.api.protocol import StopCommand, SubscribeRequest
from server.runtime import ServerRuntime
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.events import CoreEventType, EventStatus
from vibesys.run.event_journal import EventJournal as CoreEventJournal
from vibesys.run.integration import LocalRunIntegration
from vs_core.api import StopMode
from vs_runtime.api.testing import idle_environment_system
from vs_sim.api import OsThreads
from vs_sim.api.testing import (
    HANG_GUARD_S,
    ManualClock,
    SimNetwork,
    SimThreads,
    examples,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_runtime.api.infrastructure import RunControlChannel
    from vs_sim.api import Connection

_CONFIGURATION = ConfigurationError(
    ConfigurationDiagnostic(
        code="invalid_arguments",
        stage="argument_parsing",
        message="invalid argument",
        usage="usage: vibesys",
    )
)


def _fail_configuration() -> None:
    raise _CONFIGURATION


def _fail() -> None:
    message = "the run failed"
    raise RuntimeError(message)


def _finish() -> str:
    return "done"


def _read_events_to_end(connection: Connection) -> list[str]:
    data = b""
    while chunk := connection.recv(4096, HANG_GUARD_S):
        data += chunk
    connection.close()
    return [
        event["type"]
        for line in data.splitlines()
        if (message := json.loads(line))["type"] == "event_batch"
        for event in message["events"]
    ]


@dataclass(frozen=True)
class _Ending:
    """How the run ends, and what the server and its client see of it."""

    run: Callable[[], object]
    detach: bool
    terminal: str
    raised: type[Exception] | None


@pytest.mark.parametrize(
    "ending",
    [
        pytest.param(
            _Ending(
                _fail_configuration,
                detach=False,
                terminal="configuration_failed",
                raised=ConfigurationError,
            ),
            id="attached-config",
        ),
        pytest.param(
            _Ending(_fail, detach=False, terminal="run_failed", raised=RuntimeError),
            id="attached-failed",
        ),
        pytest.param(
            _Ending(
                _fail_configuration,
                detach=True,
                terminal="configuration_failed",
                raised=ConfigurationError,
            ),
            id="detached-config",
        ),
        pytest.param(
            _Ending(_finish, detach=True, terminal="run_finished", raised=None),
            id="detached-finished",
        ),
    ],
)
@settings(deadline=None, max_examples=examples(pr=8, nightly=300))
@given(seed=st.integers(0, 2**32))
def test_a_subscriber_reads_the_event_that_ends_the_run_before_the_server_closes(
    tmp_path_factory: pytest.TempPathFactory,
    ending: _Ending,
    seed: int,
) -> None:
    """However the run ends, the attached client is told why before the socket closes.

    A server that raised out of its run closed its transport as soon as the
    terminal event was journaled, so a client between two reads saw the
    connection end with no reason; only a detached server that finished let
    its clients drain first.
    """
    socket_path = tmp_path_factory.mktemp("run") / "control.sock"
    threads = SimThreads(schedule_seed=seed)

    def main() -> None:
        network = SimNetwork(threads)
        runtime = ServerRuntime(
            run_id="run-1",
            runs=production_runs(),
            socket_path=socket_path,
            detach=ending.detach,
            platform=sim_platform(threads, network=network),
        )
        seen: list[list[str]] = []
        read = threads.event()

        def subscriber() -> None:
            assert runtime.transport_listening.wait(HANG_GUARD_S)
            client = network.connect(str(socket_path))
            client.send(
                SubscribeRequest(timestamp=REQUEST_TIME, after_sequence=0)
                .model_dump_json()
                .encode()
                + b"\n"
            )
            seen.append(_read_events_to_end(client))
            read.set()

        threads.spawn(subscriber, name="subscriber", daemon=False)

        def start() -> object:
            if ending.detach:
                # A detached run starts without waiting for a client; let one attach.
                threads.sleep(0.5)
            return ending.run()

        with nullcontext() if ending.raised is None else pytest.raises(ending.raised):
            runtime.run(start)
        assert read.wait(HANG_GUARD_S), "the subscriber never saw the connection end"
        assert ending.terminal in seen[0], seen

    threads.run(main)


# --- one subscriber over a whole run ---------------------------------------------------


class _Subscription:
    """One client's event stream, read a message at a time on ``SimNetwork``."""

    def __init__(self, network: SimNetwork, socket_path: Path) -> None:
        self._connection = network.connect(str(socket_path), HANG_GUARD_S)
        self._connection.send(
            SubscribeRequest(timestamp=REQUEST_TIME, after_sequence=0).model_dump_json().encode()
            + b"\n"
        )
        self._buffer = b""

    def read(self) -> dict[str, Any]:
        """The next message; fails the test if the server hangs up first."""
        while b"\n" not in self._buffer:
            chunk = self._connection.recv(4096, HANG_GUARD_S)
            assert chunk, "the server hung up mid-stream"
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line)

    def close(self) -> None:
        self._connection.close()


def _collect_until(
    runtime: ServerRuntime,
    network: SimNetwork,
    socket_path: Path,
    done: Callable[[list[dict[str, Any]]], bool],
    received: list[dict[str, Any]],
) -> None:
    """Subscribe once the server listens, keep every event, hang up once ``done``."""
    assert runtime.transport_listening.wait(HANG_GUARD_S)
    subscription = _Subscription(network, socket_path)
    try:
        while True:
            events = subscription.read().get("events", [])
            received.extend(events)
            if done(events):
                return
    finally:
        subscription.close()


def _has(event_type: str) -> Callable[[list[dict[str, Any]]], bool]:
    return lambda events: any(event["type"] == event_type for event in events)


def _on_sim(seed: int, body: Callable[[SimThreads, SimNetwork], None]) -> None:
    threads = SimThreads(schedule_seed=seed)
    threads.run(lambda: body(threads, SimNetwork(threads)))


def _runtime(socket_path: Path, threads: SimThreads, network: SimNetwork) -> ServerRuntime:
    return ServerRuntime(
        run_id="run-1",
        runs=production_runs(),
        socket_path=socket_path,
        platform=sim_platform(threads, network=network),
    )


_SEEDS = settings(deadline=None, max_examples=examples(pr=3, nightly=100))


@_SEEDS
@given(seed=st.integers(0, 2**32))
def test_runtime_streams_success_before_client_disconnect(
    tmp_path_factory: pytest.TempPathFactory, seed: int
) -> None:
    socket_path = tmp_path_factory.mktemp("run") / "control.sock"

    def body(threads: SimThreads, network: SimNetwork) -> None:
        runtime = _runtime(socket_path, threads, network)
        received: list[dict[str, Any]] = []
        subscriber = threads.spawn(
            lambda: _collect_until(runtime, network, socket_path, _has("run_finished"), received),
            name="subscriber",
            daemon=False,
        )

        value = runtime.run(lambda: "ran")

        subscriber.join(HANG_GUARD_S)
        assert value == "ran"
        assert not subscriber.is_alive()
        assert any(event["type"] == "server_ready" for event in received)
        assert sum(event["type"] == "run_finished" for event in received) == 1
        assert not socket_path.exists()

    _on_sim(seed, body)


@_SEEDS
@given(seed=st.integers(0, 2**32))
def test_runtime_waits_for_reconnected_subscriber_before_teardown(
    tmp_path_factory: pytest.TempPathFactory, seed: int
) -> None:
    """A subscriber that reconnects must outlive the run's terminal event.

    The first subscription drops before the run finishes; the runtime must not
    treat that as "the client is gone" while the second subscription is live.
    """
    socket_path = tmp_path_factory.mktemp("run") / "control.sock"

    def body(threads: SimThreads, network: SimNetwork) -> None:
        runtime = _runtime(socket_path, threads, network)
        release_run = threads.event()
        order: list[str] = []

        def drive_reconnect() -> None:
            assert runtime.transport_listening.wait(HANG_GUARD_S)
            first = _Subscription(network, socket_path)
            assert first.read()["type"] == "subscribed"
            first.close()
            second = _Subscription(network, socket_path)
            assert second.read()["type"] == "subscribed"
            release_run.set()
            while not _has("run_finished")(second.read().get("events", [])):
                pass
            # The stream is still open here; the runtime may return only after
            # this client has disconnected.
            order.append("second subscription closing")
            second.close()

        clients = threads.spawn(drive_reconnect, name="clients", daemon=False)

        def run() -> str:
            assert release_run.wait(HANG_GUARD_S)
            return "ran"

        value = runtime.run(run)
        order.append("runtime returned")

        clients.join(HANG_GUARD_S)
        assert value == "ran"
        assert not clients.is_alive()
        assert order == ["second subscription closing", "runtime returned"]
        assert not socket_path.exists()

    _on_sim(seed, body)


class _SessionControlStub:
    """The stop half of ``vibesys.api.RunControl`` over a ``RunControlChannel``."""

    def __init__(self, control: RunControlChannel) -> None:
        self._control = control

    def steer(self, text: str) -> None:
        self._control.queue_steer(text)

    def pause(self) -> None:
        self._control.request_pause()

    def resume(self) -> None:
        self._control.resume()

    def resume_with_fallback(self) -> None:
        self._control.resume_with_fallback()

    def stop(self) -> None:
        self._control.request_stop(StopMode.DRAIN)


@_SEEDS
@given(seed=st.integers(0, 2**32))
def test_runtime_returns_cleanly_after_an_operator_stop(
    tmp_path_factory: pytest.TempPathFactory, seed: int
) -> None:
    """An in-band `/stop` ends the backend without a failure record.

    The journal's terminal record is the `stopped` status change; no
    `run_finished`, `run_failed`, or `run_interrupted` event follows it.
    """
    socket_path = tmp_path_factory.mktemp("run") / "control.sock"

    def stopped(events: list[dict[str, Any]]) -> bool:
        return any(
            event["type"] == "run_status_changed" and event["data"]["status"] == "stopped"
            for event in events
        )

    def body(threads: SimThreads, network: SimNetwork) -> None:
        runtime = _runtime(socket_path, threads, network)
        received: list[dict[str, Any]] = []
        subscriber = threads.spawn(
            lambda: _collect_until(runtime, network, socket_path, stopped, received),
            name="subscriber",
            daemon=False,
        )
        integration = LocalRunIntegration(
            clock=ManualClock(),
            git=REAL_GIT,
            github=NO_GITHUB,
            threads=OsThreads(),
            system=idle_environment_system(),
        )
        integration.events.subscribe(runtime.integration.project_event)

        def run() -> str:
            control = _SessionControlStub(integration.control)
            runtime.controls.install(control)
            try:
                runtime.api.execute(StopCommand(timestamp=REQUEST_TIME))
                integration.control.raise_if_stopped()
                return "unreachable"
            finally:
                runtime.controls.release(control)

        try:
            value = runtime.run(run)
        finally:
            integration.close()

        subscriber.join(HANG_GUARD_S)
        assert value is None
        assert not subscriber.is_alive()
        terminal = ("run_finished", "run_failed", "run_interrupted")
        assert not any(event["type"] in terminal for event in received)
        statuses = [
            event["data"]["status"] for event in received if event["type"] == "run_status_changed"
        ]
        # The stop arrives before the run is ready, so the run holds it while
        # starting and core lands it there: the run never reports running.
        assert statuses == ["stopped"]
        assert not socket_path.exists()

    _on_sim(seed, body)


@_SEEDS
@given(seed=st.integers(0, 2**32))
def test_runtime_does_not_duplicate_core_terminal_event(
    tmp_path_factory: pytest.TempPathFactory, seed: int
) -> None:
    socket_path = tmp_path_factory.mktemp("run") / "control.sock"

    def body(threads: SimThreads, network: SimNetwork) -> None:
        runtime = _runtime(socket_path, threads, network)
        received: list[dict[str, Any]] = []
        subscriber = threads.spawn(
            lambda: _collect_until(runtime, network, socket_path, _has("run_finished"), received),
            name="subscriber",
            daemon=False,
        )
        core_events = CoreEventJournal(clock=ManualClock(), threads=OsThreads())
        core_events.subscribe(runtime.integration.project_event)

        runtime.run(
            lambda: core_events.emit(CoreEventType.RUN_FINISHED, status=EventStatus.COMPLETED)
        )

        subscriber.join(HANG_GUARD_S)
        assert not subscriber.is_alive()
        assert sum(event["type"] == "run_finished" for event in received) == 1
        assert sum(event.type.value == "run_finished" for event in runtime.journal.read()) == 1

    _on_sim(seed, body)


@_SEEDS
@given(seed=st.integers(0, 2**32))
def test_runtime_streams_configuration_failure_without_run_failure(
    tmp_path_factory: pytest.TempPathFactory, seed: int
) -> None:
    socket_path = tmp_path_factory.mktemp("run") / "control.sock"
    failure = ConfigurationError(
        ConfigurationDiagnostic(
            code="invalid_arguments",
            stage="argument_parsing",
            message="unknown token=super-secret option --bad",
            usage="usage: vibesys --token=super-secret",
        )
    )

    def fail() -> None:
        raise failure

    def body(threads: SimThreads, network: SimNetwork) -> None:
        runtime = _runtime(socket_path, threads, network)
        received: list[dict[str, Any]] = []
        subscriber = threads.spawn(
            lambda: _collect_until(
                runtime, network, socket_path, _has("configuration_failed"), received
            ),
            name="subscriber",
            daemon=False,
        )

        with pytest.raises(ConfigurationError) as raised:
            runtime.run(fail)

        assert raised.value is failure
        subscriber.join(HANG_GUARD_S)
        assert not subscriber.is_alive()
        event = next(event for event in received if event["type"] == "configuration_failed")
        assert event["data"]["code"] == "invalid_arguments"
        assert event["data"]["message"] == "unknown token=[REDACTED] option --bad"
        assert event["data"]["usage"] == "usage: vibesys --token=[REDACTED]"
        assert not any(event["type"] == "run_failed" for event in received)

    _on_sim(seed, body)
