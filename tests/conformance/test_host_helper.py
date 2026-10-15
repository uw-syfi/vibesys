"""The host helper conformance runner, in process: ``runners/host-helper.json``.

Each scenario is one channel served by :func:`server.host_helper.serve_channel` over
Fakes on simulated threads, with an empty registry and no project at the paths the
scenarios name. The ``logs`` group's host also has a log store holding the streams
:func:`_seeded_logs` writes. ``tests/e2e/test_host_helper.py`` replays the other
groups over stdio against a real helper command.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any

import pytest
from tests.conformance.replay import load_scenario, run_steps, runner_group
from tests.server.host_helper_support import (
    ByteBuffer,
    ChannelClient,
    FakeLogReader,
    fake_host,
    serve_on,
    with_logs,
)

from server.host_api import LogPipe, LogStreamOutcome
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue

_RUNNER = "host-helper"
_SCENARIOS = tuple(
    itertools.chain.from_iterable(
        runner_group(_RUNNER, group) for group in ("handshake", "registry", "host-paths", "logs")
    )
)
_LOGS = frozenset(runner_group(_RUNNER, "logs"))
_INSTANCE = "0123456789ab"


def _seeded_logs(threads: SimThreads) -> FakeLogReader:
    """Stream 000001: an open 66-byte image build on stdout. Stream 000002: a failed
    42-byte server stream on stderr, evicted before offset 14."""
    logs = FakeLogReader(threads)
    build_owner: dict[str, JsonValue] = {"kind": "startup_step", "step": "image_build"}
    logs.open(_INSTANCE, "000001", build_owner, "docker build .")
    logs.write(
        _INSTANCE,
        "000001",
        b"Step 1/8 : FROM python\nStep 2/8 : WORKDIR /app\nStep 3/8 : RUN make",
    )
    logs.open(_INSTANCE, "000002", {"kind": "server_process"}, "vibesys")
    server = b"".join(b"line %d\n" % line for line in range(6))
    logs.write(_INSTANCE, "000002", server, LogPipe.STDERR)
    logs.evict(_INSTANCE, "000002", 14)
    logs.close(_INSTANCE, "000002", LogStreamOutcome.FAILED)
    return logs


class _Connection:
    def __init__(self, client: ChannelClient) -> None:
        self._client = client

    def send(self, frame: Mapping[str, Any]) -> None:
        self._client.send(dict(frame))

    def receive(self) -> dict[str, Any]:
        return self._client.receive()

    def close(self) -> None:
        self._client.close()


@pytest.mark.parametrize("name", _SCENARIOS)
@pytest.mark.parametrize("seed", range(3))
def test_scenario_replays_against_the_helper_in_process(name: str, seed: int) -> None:
    scenario = load_scenario(name)
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads)

    def main() -> int | None:
        client = ChannelClient(threads)
        host = fake_host(threads, network)
        if name in _LOGS:
            host = with_logs(host, _seeded_logs(threads), threads)
        worker = serve_on(threads, client, ByteBuffer(), host)
        run_steps(_Connection(client), scenario)
        client.close()
        worker.join(HANG_GUARD_S)
        return client.status

    assert threads.run(main) == 0
