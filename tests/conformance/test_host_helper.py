"""The host helper conformance runner, in process: ``runners/host-helper.json``.

Each scenario is one channel served by :func:`server.host_helper.serve_channel` over
Fakes on simulated threads, with an empty registry and no project at the paths the
scenarios name. The ``logs`` group's host also has a log store holding the streams
``fixtures/helper-log-store.json`` describes. ``tests/e2e/test_host_helper.py`` replays the other
groups over stdio against a real helper command.
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path
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

_RUNNER = "host-helper"
_SCENARIOS = tuple(
    itertools.chain.from_iterable(
        runner_group(_RUNNER, group)
        for group in ("handshake", "registry", "host-paths", "examples", "logs")
    )
)
_LOGS = frozenset(runner_group(_RUNNER, "logs"))
_LOG_STORE = Path(__file__).parent / "fixtures" / "helper-log-store.json"


def _seeded_logs(threads: SimThreads) -> FakeLogReader:
    """The log store ``fixtures/helper-log-store.json`` describes, which every runner seeds."""
    fixture = json.loads(_LOG_STORE.read_text())
    logs = FakeLogReader(threads)
    for instance_id, streams in fixture["instances"].items():
        for stream in streams:
            logs.open(instance_id, stream["id"], stream["owner"], stream["command"])
            for write in stream["writes"]:
                logs.write(
                    instance_id, stream["id"], write["text"].encode(), LogPipe(write["pipe"])
                )
            logs.evict(instance_id, stream["id"], stream["evict_before"])
            if stream["outcome"] is not None:
                logs.close(instance_id, stream["id"], LogStreamOutcome(stream["outcome"]))
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
