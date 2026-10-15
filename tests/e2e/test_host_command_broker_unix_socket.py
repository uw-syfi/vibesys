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
    AgentGpuConfig,
    BrokerTransport,
    GpuCommand,
    GpuCommands,
    GpuJobRequest,
    HostCommandBroker,
    RunRoots,
)

# test-isolation: the client is a single-file CLI that is intentionally absent from the library API.
from vs_sandbox.host_command_client import Stop, execute
from vs_sim.api import OsThreads, UnixNetwork
from vs_sim.api.testing import HANG_GUARD_S, join_or_fail, wait_or_fail

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from vs_sim.api import Event


class _NoConfinement:
    """Runs the job's argv as given: this test is about the socket, not about confinement."""

    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        del workspace
        return list(argv)


class _Launcher:
    """A job launcher that writes one line, then optionally waits to be cancelled."""

    def __init__(self, *, block: bool = False) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self._block = block

    def run(
        self,
        request: GpuJobRequest,
        command: GpuCommand,
        *,
        write: Callable[[bytes], None],
        cancel: Event,
    ) -> int:
        del request, command
        write(b"gpu job\n")
        self.started.set()
        if self._block:
            wait_or_fail(cancel, "the broker to cancel the job")
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
def _serving(directory: Path, launcher: _Launcher) -> Iterator[HostCommandBroker]:
    workspace = directory / "workspace"
    workspace.mkdir()
    config = AgentGpuConfig.model_validate(
        {"partitions": ("main",), "max_gpus": 8, "max_time_minutes": 60}
    )
    broker = HostCommandBroker(
        directory / "broker.sock",
        roots=RunRoots((workspace,)),
        gpu=GpuCommands(config, _NoConfinement(), {}, launcher=launcher),
        transport=BrokerTransport(UnixNetwork(), OsThreads()),
    )
    broker.start()
    try:
        yield broker
    finally:
        broker.close()


def test_a_gpu_job_runs_through_the_real_socket(
    monkeypatch: pytest.MonkeyPatch, capsysbinary: pytest.CaptureFixture[bytes]
) -> None:
    with _short_directory() as directory, _serving(directory, _Launcher()) as broker:
        monkeypatch.chdir(directory / "workspace")
        monkeypatch.setenv(COMMAND_BROKER_SOCKET_ENV, str(broker.socket_path))
        monkeypatch.setenv(COMMAND_BROKER_TOKEN_ENV, broker.token)

        status = execute(["--", "nvidia-smi"], stop=Stop())

    assert status == 3
    assert capsysbinary.readouterr().out == b"gpu job\n"
    assert not broker.socket_path.exists()


def test_hanging_up_cancels_a_running_job() -> None:
    launcher = _Launcher(block=True)
    with _short_directory() as directory, _serving(directory, launcher) as broker:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(HANG_GUARD_S)
        client.connect(str(broker.socket_path))
        call = {
            "op": "gpu",
            "token": broker.token,
            "argv": ["nvidia-smi"],
            "cwd": str(directory / "workspace"),
            "gpus": None,
            "time_minutes": None,
            "env": {},
        }
        client.sendall(json.dumps(call).encode() + b"\n")
        wait_or_fail(launcher.started, "the job to start")
        client.close()

        wait_or_fail(launcher.cancelled, "the hang-up to cancel the job")


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
