"""A Slurm run environment for tests: a Docker editor with an in-memory image build."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api.request import RunEnvironmentSpec
from vs_agent.api.testing import FakeDockerBuildRunner

if TYPE_CHECKING:
    from pathlib import Path

_SSH_CONFIG = """[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/vibesys"

[slurm.transport]
kind = "ssh"
host = "test-cluster"

[vibesys]
remote_python = "/remote/venv/bin/python"
"""


def slurm_environment(directory: Path) -> RunEnvironmentSpec:
    """Write an operator Slurm config under *directory* and select it.

    Pair it with a backend whose containers run on the host, as
    ``tests.support.docker_environment.host_container_backend`` builds.
    """
    config_path = directory / "operator-slurm.toml"
    config_path.write_text(_SSH_CONFIG, encoding="utf-8")
    return RunEnvironmentSpec(
        "slurm", {"config_path": str(config_path), "build_runner": FakeDockerBuildRunner()}
    )
