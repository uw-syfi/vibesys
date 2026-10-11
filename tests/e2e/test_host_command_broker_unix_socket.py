"""The host command broker and its client over a real ``AF_UNIX`` socket and real threads.

The unit tests in ``libs/vs-sandbox/tests`` run the same broker and client on the
simulated network. What only a real socket shows is here: the kernel's buffering when a
broker drops a connection mid-request, and the hang-up that cancels a running job. No
Docker or Slurm is involved, so these run in every environment.
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import (
    COMMAND_BROKER_SOCKET_ENV,
    COMMAND_BROKER_TOKEN_ENV,
    GateKind,
    Gates,
    HostCommandBroker,
    RunRoots,
)

# test-isolation: the client is a single-file CLI that is intentionally absent from the library API.
from vs_sandbox.host_command_client import Stop, execute
from vs_sim.api.testing import HANG_GUARD_S, join_or_fail, wait_or_fail

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from vs_sim.api import Event


class _Gates:
    """A gate runner that writes one line, then optionally waits to be cancelled."""

    def __init__(self, *, block: bool = False) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self._block = block

    def run(
        self,
        kind: GateKind,
        arguments: Sequence[str],
        *,
        cwd: Path,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        del arguments, cwd
        write(f"gate {kind.value}\n".encode())
        self.started.set()
        if self._block:
            wait_or_fail(cancel, "the broker to cancel the gate")
            self.cancelled.set()
        return 3


@contextmanager
def _short_directory() -> Iterator[Path]:
    # Unix socket paths are limited to about 100 bytes, so not under pytest's long tmp_path.
    directory = Path(tempfile.mkdtemp(prefix="vs-broker-"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@contextmanager
def _serving(directory: Path, gates: _Gates) -> Iterator[HostCommandBroker]:
    workspace = directory / "workspace"
    workspace.mkdir()
    broker = HostCommandBroker(
        directory / "broker.sock", roots=RunRoots((workspace,)), gates=Gates(gates)
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.close()


def test_a_gate_runs_through_the_real_socket(
    monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with _short_directory() as directory, _serving(directory, _Gates()) as broker:
        monkeypatch.chdir(directory / "workspace")
        monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, str(broker.socket_path))
        monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, broker.token)

        status = execute(["--gate", "accuracy"], stop=Stop())

    assert status == 3
    assert capsysbinary.readouterr().out == b"gate accuracy\n"
    assert not broker.socket_path.exists()


def test_hanging_up_cancels_a_running_gate() -> None:
    gates = _Gates(block=True)
    with _short_directory() as directory, _serving(directory, gates) as broker:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(HANG_GUARD_S)
        client.connect(str(broker.socket_path))
        call = {
            "op": "gate",
            "token": broker.token,
            "kind": "accuracy",
            "arguments": [],
            "cwd": str(directory / "workspace"),
        }
        client.sendall(json.dumps(call).encode() + b"\n")
        wait_or_fail(gates.started, "the gate to start")
        client.close()

        wait_or_fail(gates.cancelled, "the hang-up to cancel the gate")


@pytest.mark.parametrize("argument_bytes", [1, 4 * 1024 * 1024])
def test_a_broker_that_drops_a_request_unread_is_a_dropped_connection(
    argument_bytes: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A request larger than the socket buffer blocks the send until the broker closes it."""
    with _short_directory() as directory:
        path = directory / "dead.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)

        def accept_and_drop() -> None:
            server.settimeout(HANG_GUARD_S)
            connection, _ = server.accept()
            connection.close()

        thread = threading.Thread(target=accept_and_drop)
        thread.start()
        monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, str(path))
        monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, "t")
        try:
            status = execute(["--", "x" * argument_bytes], stop=Stop())
        finally:
            join_or_fail(thread)
            server.close()

    assert status == 1
    assert "closed the connection" in capsys.readouterr().err
