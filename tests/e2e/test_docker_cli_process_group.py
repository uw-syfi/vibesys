"""Control-command adapters own their local process groups on a real POSIX host.

Ctrl-C signals the terminal's foreground process group; a ``docker stop`` started in
that group dies mid-flight. Only a real child process can report its own session, so
this belongs to the real-system tier. Synchronized descendants also hold a FIFO writer
open, proving Docker and Slurm timeout cleanup reaches the whole locally owned group.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from vs_sandbox.api import SubprocessDockerCli
from vs_sandbox.api.slurm import SlurmProcessBroker, run_brokered_process

# test-isolation: the synchronized real-process contract injects the package's process owner
# into public Docker and Slurm boundaries so timeout begins only after the descendant is ready.
from vs_sandbox.process_execution import (
    CapturedProcessRequest,
    OwnedProcessRunner,
    PopenProcess,
)
from vs_slurm.api import SlurmConfig, SlurmSshTransport

_HANG_GUARD_SECONDS = 30.0
_READY_STDOUT = "descendant stdout before timeout\n"
_READY_STDERR = "descendant stderr before timeout\n"


class _InitialWaitFailureProcess(PopenProcess):
    """Raise once before delegating to the real POSIX process handle."""

    def __init__(
        self,
        process: subprocess.Popen[str],
        failure: BaseException,
        *,
        stdin: str | None,
    ) -> None:
        super().__init__(process, stdin=stdin)
        self._failure: BaseException | None = failure

    def wait(self, timeout: float | None) -> tuple[str, str] | None:
        failure, self._failure = self._failure, None
        if failure is not None:
            raise failure
        return super().wait(timeout)


@dataclass(frozen=True)
class _ReadyLauncher:
    """Start a real group, then synchronize on its descendant's FIFO byte."""

    ready_reader: int
    initial_wait_failure: BaseException | None = None
    started: list[subprocess.Popen[str]] = field(default_factory=list, compare=False)

    def start(self, request: CapturedProcessRequest) -> PopenProcess:
        process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-194301 [S603]; this real contract executes its generated interpreter argv through the production process-owner seam.
            # > A shell wrapper would weaken argv separation, while patching the
            # > production launcher would bypass the public injection contract.
            request.argv,
            env=dict(request.env) if request.env is not None else None,
            cwd=request.cwd,
            stdin=subprocess.PIPE if request.stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=request.start_new_session,
            process_group=-1 if request.start_new_session else 0,
        )
        self.started.append(process)
        owned = (
            PopenProcess(process, stdin=request.stdin)
            if self.initial_wait_failure is None
            else _InitialWaitFailureProcess(
                process,
                self.initial_wait_failure,
                stdin=request.stdin,
            )
        )
        ready, _, _ = select.select([self.ready_reader], [], [], _HANG_GUARD_SECONDS)
        if ready and os.read(self.ready_reader, 1) == b"R":
            return owned
        owned.signal_group(signal.SIGKILL)
        owned.wait(_HANG_GUARD_SECONDS)
        owned.close()
        pytest.fail("descendant did not become ready before the hang guard")


def _descendant_wrapper(ready_fifo: Path) -> str:
    descendant = (
        "import os,sys,time; "
        f"sys.stdout.write({_READY_STDOUT!r}); sys.stdout.flush(); "
        f"sys.stderr.write({_READY_STDERR!r}); sys.stderr.flush(); "
        "fd=os.open(sys.argv[1], os.O_WRONLY); "
        "os.write(fd, b'R'); "
        "time.sleep(600)"
    )
    return (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable, '-c', {descendant!r}, {str(ready_fifo)!r}]); "
        "time.sleep(600)"
    )


def _ready_reader(ready_fifo: Path) -> int:
    os.mkfifo(ready_fifo)
    return os.open(ready_fifo, os.O_RDONLY | os.O_NONBLOCK)


def _assert_descendant_writer_closed(reader: int) -> None:
    os.set_blocking(reader, False)
    try:
        remaining = os.read(reader, 1)
    except BlockingIOError:
        pytest.fail("descendant still holds the readiness pipe after timeout cleanup")
    assert remaining == b""


def _settle_started_processes(processes: list[subprocess.Popen[str]]) -> None:
    """Keep a failed regression from leaving its real process group behind."""
    for process in processes:
        if process.returncode is not None:
            continue
        with suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
        try:
            process.wait(timeout=_HANG_GUARD_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=_HANG_GUARD_SECONDS)


def test_docker_lifecycle_commands_run_outside_the_terminals_process_group() -> None:
    probe = "import os; print(os.getsid(0) == os.getpid())"

    result = SubprocessDockerCli().run([sys.executable, "-c", probe], timeout_seconds=30)

    assert result.stdout.strip() == "True"


def test_docker_timeout_kills_the_ready_descendant_and_closes_its_pipe(tmp_path: Path) -> None:
    ready_fifo = tmp_path / "docker-descendant-ready"
    reader = _ready_reader(ready_fifo)
    launcher = _ReadyLauncher(reader)
    try:
        processes = OwnedProcessRunner(processes=launcher)
        with pytest.raises(subprocess.TimeoutExpired) as raised:
            SubprocessDockerCli(processes=processes).run(
                [sys.executable, "-c", _descendant_wrapper(ready_fifo)],
                timeout_seconds=0.1,
            )
        assert raised.value.output == _READY_STDOUT.encode()
        assert raised.value.stderr == _READY_STDERR.encode()
        _assert_descendant_writer_closed(reader)
    finally:
        _settle_started_processes(launcher.started)
        os.close(reader)


def test_docker_interruption_kills_the_unreaped_ready_process_group(tmp_path: Path) -> None:
    ready_fifo = tmp_path / "docker-interrupted-descendant-ready"
    reader = _ready_reader(ready_fifo)
    interruption = KeyboardInterrupt("interrupted wait")
    launcher = _ReadyLauncher(reader, initial_wait_failure=interruption)
    try:
        processes = OwnedProcessRunner(processes=launcher)
        with pytest.raises(KeyboardInterrupt) as raised:
            SubprocessDockerCli(processes=processes).run(
                [sys.executable, "-c", _descendant_wrapper(ready_fifo)],
                timeout_seconds=30,
            )
        assert raised.value is interruption
        _assert_descendant_writer_closed(reader)
    finally:
        _settle_started_processes(launcher.started)
        os.close(reader)


def test_slurm_broker_timeout_kills_the_ready_descendant_and_closes_its_pipe(
    tmp_path: Path,
) -> None:
    ready_fifo = tmp_path / "slurm-descendant-ready"
    reader = _ready_reader(ready_fifo)
    wrapper = _descendant_wrapper(ready_fifo)
    transport = SlurmSshTransport(
        host="cluster",
        ssh_command=(sys.executable, "-c", wrapper),
    )
    config = SlurmConfig(
        name="cluster",
        remote_workspace_root="/remote/vibesys",
        transport=transport,
        transport_timeout_seconds=1,
    )
    launcher = _ReadyLauncher(reader)
    broker = SlurmProcessBroker(
        config,
        tmp_path / "broker.sock",
        local_roots=(tmp_path,),
        processes=OwnedProcessRunner(processes=launcher),
    )
    broker.start()
    try:
        with pytest.raises(PermissionError, match="timed out"):
            run_brokered_process(
                broker.socket_path,
                broker.token,
                (*transport.ssh_command, "--", "cluster", "hostname"),
                stdin=None,
                timeout=5,
            )
        _assert_descendant_writer_closed(reader)
    finally:
        broker.close()
        _settle_started_processes(launcher.started)
        os.close(reader)


def test_slurm_broker_preserves_stdin_environment_cwd_and_complete_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_key = "VIBESYS_BROKER_PROCESS_CONTRACT"
    monkeypatch.setenv(environment_key, "present")
    report = (
        "import os,sys; "
        "data=sys.stdin.read(); "
        f"print(os.environ[{environment_key!r}], os.getcwd(), data, sep='|'); "
        "print('complete stderr', file=sys.stderr)"
    )
    transport = SlurmSshTransport(
        host="cluster",
        ssh_command=(sys.executable, "-c", report),
    )
    config = SlurmConfig(
        name="cluster",
        remote_workspace_root="/remote/vibesys",
        transport=transport,
        transport_timeout_seconds=30,
    )
    broker = SlurmProcessBroker(config, tmp_path / "broker.sock", local_roots=(tmp_path,))
    broker.start()
    try:
        result = run_brokered_process(
            broker.socket_path,
            broker.token,
            (*transport.ssh_command, "--", "cluster", "hostname"),
            stdin="complete stdin",
            timeout=30,
        )
    finally:
        broker.close()

    assert result.returncode == 0
    assert result.stdout == f"present|{Path.cwd()}|complete stdin\n"
    assert result.stderr == "complete stderr\n"
