"""A read-only server of a finished run: it ends once no client holds it, and it refuses steers.

``ServerRuntime`` serves the journal of a run that ended (``read_only_log``) on ``SimThreads``
and ``SimNetwork``, so every wait is virtual: the idle bound passes by advancing the simulated
clock, never by waiting for it.
"""

from __future__ import annotations

import json
from functools import partial
from typing import TYPE_CHECKING, cast

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.server.support import (
    CONTROL_KEY,
    REQUEST_TIME,
    build_server_parts,
    operator_dialer,
    sim_platform,
)

from server.api.protocol import ControlCommand
from server.runtime import ServerRuntime
from vibesys.api import SteerRequest
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    import pytest

    from vibesys.api import Runs

IDLE_SECONDS = 30.0
POLL_SLACK_SECONDS = 1.0
"""The server looks at its clients every tenth of a second; this is far more than it needs."""
_SEEDS = settings(deadline=None, max_examples=examples(pr=6, nightly=200))


class _NoRuns:
    """The run service of a server that starts no run: a read-only server only serves a journal."""

    def start(self, request: object) -> object:
        message = f"a read-only server starts no run: {request!r}"
        raise AssertionError(message)

    resume = start


def _finished_journal(directory: Path) -> Path:
    """The log directory of a run that ended, as a finished run leaves it."""
    log_dir = directory / "j"
    parts = build_server_parts()
    parts.journal.attach(log_dir, run_id="finished-run")
    parts.controller.finish()
    parts.close()
    return log_dir


def _reader(
    log_dir: Path, socket_path: Path, threads: SimThreads, network: SimNetwork
) -> ServerRuntime:
    return ServerRuntime(
        runs=cast("Runs", _NoRuns()),
        socket_path=socket_path,
        read_only_log=log_dir,
        reader_idle_seconds=IDLE_SECONDS,
        platform=sim_platform(threads, network=network),
        control_key=CONTROL_KEY,
    )


def _serve(
    seed: int,
    directory: Path,
    clients: Callable[[ServerRuntime, SimThreads, SimNetwork, Path], None],
) -> tuple[float, float]:
    """Serve the finished run's journal read-only until it ends: when it began and when it ended."""
    log_dir = _finished_journal(directory)
    socket_path = directory / "control.sock"
    threads = SimThreads(schedule_seed=seed)
    ended: list[float] = []
    began: list[float] = []

    def main() -> None:
        began.append(threads.now())
        network = SimNetwork(threads)
        runtime = _reader(log_dir, socket_path, threads, network)
        threads.spawn(
            lambda: clients(runtime, threads, network, socket_path), name="clients", daemon=False
        )
        assert runtime.run(lambda: None) is None
        ended.append(threads.now())

    threads.run(main)
    return began[0], ended[0]


@_SEEDS
@given(seed=st.integers(0, 2**32))
def test_a_reader_nobody_connects_to_ends_after_the_idle_bound(
    tmp_path_factory: pytest.TempPathFactory, seed: int
) -> None:
    directory = tmp_path_factory.mktemp("r")
    began_at, ended_at = _serve(seed, directory, lambda *_: None)
    assert IDLE_SECONDS <= ended_at - began_at <= IDLE_SECONDS + POLL_SLACK_SECONDS


@_SEEDS
@given(
    seed=st.integers(0, 2**32),
    visits=st.lists(st.tuples(st.integers(0, 20), st.integers(1, 20)), min_size=1, max_size=4),
)
def test_a_reader_ends_only_after_its_last_client_left_and_the_idle_bound_passed(
    tmp_path_factory: pytest.TempPathFactory, seed: int, visits: list[tuple[int, int]]
) -> None:
    """Whatever clients come and go, the server serves while one holds it and ends after."""
    directory = tmp_path_factory.mktemp("r")
    left_at: list[float] = []

    def clients(
        runtime: ServerRuntime, threads: SimThreads, network: SimNetwork, socket_path: Path
    ) -> None:
        assert runtime.transport_listening.wait(HANG_GUARD_S)

        def visit(delay: int, stay: int) -> None:
            threads.sleep(float(delay))
            connection = operator_dialer(network).connect(str(socket_path), HANG_GUARD_S)
            threads.sleep(float(stay))
            connection.close()
            left_at.append(threads.now())

        workers = [
            threads.spawn(partial(visit, delay, stay), name="visit", daemon=False)
            for delay, stay in visits
        ]
        for worker in workers:
            worker.join(HANG_GUARD_S)

    _, ended_at = _serve(seed, directory, clients)
    last_left = max(left_at)
    assert last_left + IDLE_SECONDS <= ended_at <= last_left + IDLE_SECONDS + POLL_SLACK_SECONDS


@_SEEDS
@given(seed=st.integers(0, 2**32), text=st.text(min_size=1, max_size=40))
def test_a_reader_refuses_a_steer_with_a_typed_reason(
    tmp_path_factory: pytest.TempPathFactory, seed: int, text: str
) -> None:
    """A steer sent to a finished run reaches no one and is refused as ``run_read_only``."""
    directory = tmp_path_factory.mktemp("r")
    replies: list[dict[str, object]] = []

    def clients(
        runtime: ServerRuntime, _threads: SimThreads, network: SimNetwork, socket_path: Path
    ) -> None:
        assert runtime.transport_listening.wait(HANG_GUARD_S)
        connection = operator_dialer(network).connect(str(socket_path), HANG_GUARD_S)
        steer = ControlCommand(
            timestamp=REQUEST_TIME, control=SteerRequest(control_id="steer-1", text=text)
        )
        connection.send(steer.model_dump_json().encode() + b"\n")
        buffer = b""
        while b"\n" not in buffer:
            chunk = connection.recv(4096, HANG_GUARD_S)
            assert chunk, "the server hung up without answering"
            buffer += chunk
        replies.append(json.loads(buffer.split(b"\n", 1)[0]))
        connection.close()

    _serve(seed, directory, clients)
    (reply,) = replies
    assert reply["ok"] is False
    diagnostic = reply["diagnostic"]
    assert isinstance(diagnostic, dict)
    assert diagnostic["code"] == "run_read_only"
