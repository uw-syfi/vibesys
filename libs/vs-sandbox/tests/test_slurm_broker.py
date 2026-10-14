"""Public contract tests for host-owned Slurm transport access."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import BrokeredProcesses, SlurmProcessBroker
from vs_sim.api import OsProcesses, ProcessOutcome, ProcessSpec, UnixNetwork
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript
from vs_slurm.api import SlurmConfig, SlurmSshTransport

if TYPE_CHECKING:
    from pathlib import Path


def _config() -> SlurmConfig:
    return SlurmConfig(
        name="cluster",
        remote_workspace_root="/remote/vibesys",
        transport=SlurmSshTransport(host="cluster", ssh_command=("/usr/bin/printf",)),
        transport_timeout_seconds=5,
    )


def _brokered(
    broker: SlurmProcessBroker, argv: tuple[str, ...], stdin: bytes = b"", timeout: float = 5
) -> ProcessOutcome:
    processes = BrokeredProcesses(broker.socket_path, broker.token, network=UnixNetwork())
    run = processes.run(ProcessSpec(argv, input=stdin), timeout=timeout)
    assert isinstance(run, ProcessOutcome)
    return run


def test_broker_executes_only_the_configured_destination(tmp_path: Path) -> None:
    broker = SlurmProcessBroker(
        _config(), tmp_path / "broker.sock", local_roots=(tmp_path,), processes=OsProcesses()
    )
    broker.start()
    try:
        result = _brokered(broker, ("/usr/bin/printf", "--", "cluster", "hostname"))
        with pytest.raises(PermissionError, match="unauthorized destination"):
            _brokered(broker, ("/usr/bin/printf", "--", "other-cluster", "hostname"))
    finally:
        broker.close()

    assert result.returncode == 0
    assert result.stdout == b"cluster"
    assert not broker.socket_path.exists()


def test_broker_rejects_rsync_paths_outside_run_roots(tmp_path: Path) -> None:
    config = _config().model_copy(
        update={
            "transport": SlurmSshTransport(
                host="cluster",
                ssh_command=("ssh",),
                rsync_command=("rsync",),
            )
        }
    )
    broker = SlurmProcessBroker(
        config, tmp_path / "unused.sock", local_roots=(tmp_path,), processes=OsProcesses()
    )
    broker.start()
    try:
        with pytest.raises(PermissionError, match="run-owned roots"):
            _brokered(
                broker,
                (
                    "rsync",
                    "-a",
                    "-e",
                    "ssh",
                    "--",
                    "/etc/passwd",
                    "cluster:/remote/vibesys/cluster/workspace/passwd",
                ),
            )
    finally:
        broker.close()


def test_a_brokered_process_is_outside_the_callers_process_group(tmp_path: Path) -> None:
    """r14: Ctrl-C to the run's process group killed brokered transport calls."""
    report = "import os; print(os.getpgid(0), os.getpid())"
    config = SlurmConfig(
        name="cluster",
        remote_workspace_root="/remote/vibesys",
        transport=SlurmSshTransport(host="cluster", ssh_command=(sys.executable, "-c", report)),
        transport_timeout_seconds=30,
    )
    broker = SlurmProcessBroker(
        config, tmp_path / "broker.sock", local_roots=(tmp_path,), processes=OsProcesses()
    )
    broker.start()
    try:
        result = _brokered(
            broker, (sys.executable, "-c", report, "--", "cluster", "scancel 1"), timeout=30
        )
    finally:
        broker.close()

    group, pid = (int(value) for value in result.stdout.split())
    assert group == pid
    assert group != os.getpgid(0)


def test_a_broker_timeout_is_refused_after_the_transport_timeout(tmp_path: Path) -> None:
    clock = ManualClock()
    processes = FakeProcesses(
        lambda _spec: ProcessScript(stdout=b"partial-out", runs_until_signalled=True),
        pass_time=clock.advance,
    )
    broker = SlurmProcessBroker(
        _config(), tmp_path / "broker.sock", local_roots=(tmp_path,), processes=processes
    )
    broker.start()
    try:
        with pytest.raises(PermissionError, match=r"timed out after 5 seconds"):
            _brokered(broker, ("/usr/bin/printf", "--", "cluster", "hostname"), b"transport input")
    finally:
        broker.close()

    assert clock.now() == 5
    assert processes.ran == [
        ProcessSpec(("/usr/bin/printf", "--", "cluster", "hostname"), input=b"transport input")
    ]
