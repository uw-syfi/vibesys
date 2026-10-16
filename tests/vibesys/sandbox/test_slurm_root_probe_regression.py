"""Regression boundary for local Slurm root preparation."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api.infrastructure import RunEnvironmentRequest, SlurmEnvironment
from vs_runtime.api.testing import idle_environment_system
from vs_sandbox.api.slurm import load_slurm_operator_settings
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript, SimThreads
from vs_slurm.api import SlurmError

if TYPE_CHECKING:
    from pathlib import Path


def test_local_slurm_root_without_owned_worktrees_still_checks_the_shared_root(
    tmp_path: Path,
) -> None:
    """A root's empty optional path list must not suppress mandatory preparation."""
    config = tmp_path / "slurm.toml"
    config.write_text(
        f'''[slurm]
name = "test-cluster"
remote_workspace_root = "{tmp_path / "remote"}"

[slurm.transport]
kind = "local"
shell_command = ["unavailable-submit-shell"]
''',
        encoding="utf-8",
    )
    threads = SimThreads()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    processes = FakeProcesses(
        lambda _spec: ProcessScript(returncode=17),
        pass_time=ManualClock().advance,
    )
    request = RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=FakeComputeBackend(threads=threads),
        agent_backend="stub",
        cli_provider=None,
        run_id="root-with-no-owned-worktrees",
        framework_root=tmp_path,
        clock=ManualClock(),
        system=replace(idle_environment_system(), threads=threads, slurm_processes=processes),
    )

    with pytest.raises(SlurmError, match=r"remote_workspace_root.*startup probe failed"):
        SlurmEnvironment(load_slurm_operator_settings(config)).prepare(request)
