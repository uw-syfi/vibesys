"""A server's whole lifetime on the simulator: serve, end with the run, close.

``ServerRuntime`` runs on ``SimThreads`` and ``SimNetwork``, so a seed decides
every interleaving of the run's end with the subscriber's reads, and every
wait is virtual.
"""

from __future__ import annotations

import json
from contextlib import nullcontext
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from launch import default_runs
from server.api.protocol import SubscribeRequest
from server.runtime import ServerRuntime
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable

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
            runs=default_runs(),
            socket_path=socket_path,
            detach=ending.detach,
            threads=threads,
            network=network,
        )
        seen: list[list[str]] = []
        read = threads.event()

        def subscriber() -> None:
            assert runtime.transport_listening.wait(HANG_GUARD_S)
            client = network.connect(str(socket_path))
            client.send(SubscribeRequest(after_sequence=0).model_dump_json().encode() + b"\n")
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
