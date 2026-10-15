"""Control-command adapters run their commands outside the terminal's process group.

Ctrl-C signals the terminal's foreground process group; a ``docker stop`` started in
that group dies mid-flight. Only a real child process can report its own group, so
this belongs to the real-system tier. That a timeout reaches the whole group is the
``vs_sim`` ``Processes`` contract, which ``OsProcesses`` passes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vs_sandbox.api import SubprocessDockerCli
from vs_sandbox.api.slurm import BrokeredProcesses, SlurmProcessBroker
from vs_sim.api import (
    OsProcesses,
    ProcessOutcome,
    ProcessSpec,
    SubprocessPipedProcessLauncher,
    UnixNetwork,
)
from vs_slurm.api import SlurmConfig, SlurmSshTransport, slurm_transport_broker_policy

if TYPE_CHECKING:
    import pytest


def test_docker_lifecycle_commands_run_outside_the_terminals_process_group() -> None:
    probe = "import os, sys; print(os.getpgid(0) == os.getpid() != int(sys.argv[1]))"

    result = SubprocessDockerCli(
        processes=OsProcesses(), launcher=SubprocessPipedProcessLauncher()
    ).run([sys.executable, "-c", probe, str(os.getpgid(0))], timeout_seconds=30)

    assert result.stdout.strip() == "True"


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
    broker = SlurmProcessBroker(
        slurm_transport_broker_policy(config, local_roots=(tmp_path,)),
        tmp_path / "broker.sock",
        processes=OsProcesses(),
    )
    broker.start()
    try:
        result = BrokeredProcesses(broker.socket_path, broker.token, network=UnixNetwork()).run(
            ProcessSpec(
                (*transport.ssh_command, "--", "cluster", "hostname"), input=b"complete stdin"
            ),
            timeout=30,
        )
    finally:
        broker.close()

    assert result == ProcessOutcome(
        0, f"present|{Path.cwd()}|complete stdin\n".encode(), b"complete stderr\n"
    )
