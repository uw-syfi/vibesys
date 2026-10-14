"""The ``vibesys-gpu`` client against a broker that sends what its protocol does not.

The client trusts nothing the broker sends. Run against a scripted broker on a
``SimNetwork``, it answers every frame of the shared hostile corpus, and every
well-formed JSON object whose fields have the wrong type, with a typed protocol error:
a non-zero status and one line on stderr, never an uncaught exception. An exit
status outside the process range 0..255 is such an error, since the client would
otherwise exit with it wrapped modulo 256 (300 as 44, 256 as success). A broker that
streams a line without ever ending it is cut off at the frame bound, not buffered
without limit.
"""

from __future__ import annotations

import json
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import COMMAND_BROKER_SOCKET_ENV, COMMAND_BROKER_TOKEN_ENV

# test-isolation: the client is a single-file CLI that is intentionally absent from the library API.
from vs_sandbox.host_command_client import Stop, execute
from vs_sim.api.testing import (
    HANG_GUARD_S,
    SimNetwork,
    SimThreads,
    hostile_json_frames,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_sim.api import Connection

ADDRESS = "broker.sock"
_EXIT_ZERO = b'{"exit":0}'
_WRONG_TYPES = (
    ("exit-is-a-string", json.dumps({"exit": "0"}).encode()),
    ("exit-is-a-bool", json.dumps({"exit": True}).encode()),
    ("exit-is-null", json.dumps({"exit": None}).encode()),
    ("exit-wraps-to-a-success", json.dumps({"exit": 256}).encode()),
    ("exit-wraps-to-another-status", json.dumps({"exit": 300}).encode()),
    ("exit-is-negative", json.dumps({"exit": -1}).encode()),
    ("exit-is-huge", json.dumps({"exit": 10**30}).encode()),
    ("output-is-a-number", json.dumps({"output": 5}).encode()),
    ("output-is-not-base64", json.dumps({"output": "not base64!"}).encode()),
    ("a-string-naming-a-key", b'"output"'),
    ("a-list-naming-a-key", b'["exit"]'),
)
_FRAMES = (*hostile_json_frames((_EXIT_ZERO,)), *_WRONG_TYPES)
_FRAME_BOUND = 16 * 1024 * 1024


def _read_request(connection: Connection) -> None:
    received = b""
    while not received.endswith(b"\n"):
        chunk = connection.recv(1 << 16, HANG_GUARD_S)
        if not chunk:
            return
        received += chunk


def _client_against(
    broker: Callable[[Connection], None],
    monkeypatch: pytest.MonkeyPatch,
    *,
    seed: int | None,
    buffer: int | None,
) -> int:
    """The client's status against ``broker(connection)``, served on a simulated network."""
    threads = SimThreads(schedule_seed=seed)
    network = SimNetwork(threads, buffer_bytes=buffer)
    monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, ADDRESS)
    monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, "t")

    def program() -> int:
        listener = network.listen(ADDRESS)

        def serve() -> None:
            connection = listener.accept(HANG_GUARD_S)
            try:
                _read_request(connection)
                broker(connection)
            finally:
                connection.close()

        worker = threads.spawn(serve, name="broker")
        try:
            return execute(
                ["--", "true"],
                stop=Stop(),
                dial=lambda address: network.connect(address, HANG_GUARD_S),
            )
        finally:
            worker.join(HANG_GUARD_S)
            listener.close()

    return threads.run(program)


@pytest.mark.parametrize(
    ("index", "name", "frame"),
    [(index, name, frame) for index, (name, frame) in enumerate(_FRAMES)],
    ids=[name for name, _ in _FRAMES],
)
def test_a_frame_outside_the_protocol_is_a_typed_error(
    index: int,
    name: str,
    frame: bytes,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seed = None if index % 2 else index

    def broker(connection: Connection) -> None:
        with suppress(OSError):
            connection.send(frame + b"\n", HANG_GUARD_S)

    status = _client_against(broker, monkeypatch, seed=seed, buffer=None)

    assert status != 0, name
    err = capsys.readouterr().err
    assert err.startswith("vibesys-gpu: "), name
    assert err.count("\n") == 1, name


@pytest.mark.parametrize("seed", [None, 7])
def test_a_line_that_never_ends_is_cut_off_at_the_frame_bound(
    seed: int | None, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sent: list[int] = [0]
    chunk = b"x" * (1 << 16)

    def broker(connection: Connection) -> None:
        # A bounded buffer makes each send wait for the client to read, so the
        # broker stops once the client hangs up; the cap only guards a client
        # that never would.
        with suppress(OSError):
            while sent[0] < 4 * _FRAME_BOUND:
                connection.send(chunk, HANG_GUARD_S)
                sent[0] += len(chunk)

    status = _client_against(broker, monkeypatch, seed=seed, buffer=1 << 20)

    assert status == 1
    assert "longer than" in capsys.readouterr().err
    assert sent[0] < _FRAME_BOUND + 4 * (1 << 20), "the client stopped reading at the bound"


@pytest.mark.parametrize("exit_status", [0, 1, 2, 42, 128 + 9, 255])
def test_an_exit_status_in_the_process_range_is_the_clients_own(
    exit_status: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broker(connection: Connection) -> None:
        connection.send(json.dumps({"exit": exit_status}).encode() + b"\n", HANG_GUARD_S)

    status = _client_against(broker, monkeypatch, seed=exit_status, buffer=None)

    assert status == exit_status
    assert capsys.readouterr().err == ""
