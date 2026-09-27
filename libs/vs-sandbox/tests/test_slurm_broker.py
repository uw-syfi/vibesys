"""Public contract tests for host-owned Slurm transport access."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import SlurmProcessBroker, run_brokered_process
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


def test_broker_executes_only_the_configured_destination(tmp_path: Path) -> None:
    broker = SlurmProcessBroker(_config(), tmp_path / "broker.sock", local_roots=(tmp_path,))
    broker.start()
    try:
        result = run_brokered_process(
            broker.socket_path,
            broker.token,
            ("/usr/bin/printf", "--", "cluster", "hostname"),
            stdin=None,
            timeout=5,
        )
        with pytest.raises(PermissionError, match="unauthorized destination"):
            run_brokered_process(
                broker.socket_path,
                broker.token,
                ("/usr/bin/printf", "--", "other-cluster", "hostname"),
                stdin=None,
                timeout=5,
            )
    finally:
        broker.close()

    assert result.returncode == 0
    assert result.stdout == "cluster"
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
    broker = SlurmProcessBroker(config, tmp_path / "unused.sock", local_roots=(tmp_path,))
    broker.start()
    try:
        with pytest.raises(PermissionError, match="run-owned roots"):
            run_brokered_process(
                broker.socket_path,
                broker.token,
                (
                    "rsync",
                    "-a",
                    "-e",
                    "ssh",
                    "--",
                    "/etc/passwd",
                    "cluster:/remote/vibesys/cluster/workspace/passwd",
                ),
                stdin=None,
                timeout=5,
            )
    finally:
        broker.close()
