"""A Slurm run environment for tests: a Docker editor with an in-memory image build."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from vibesys.api.request import RunEnvironmentSpec
from vs_agent.api.testing import FakeDockerBuildRunner

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.run.contracts import RunRequest

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


def with_fake_image_build(request: RunRequest) -> RunRequest:
    """Return *request* with an in-memory agent image build when it selects Slurm.

    A request built from the command line cannot carry the unrecorded build
    seam; the agent runs in a container, and the image build is its only
    external process. Any other environment is returned unchanged.
    """
    environment = request.run_environment
    if environment is None or environment.name != "slurm":
        return request
    options = {"build_runner": FakeDockerBuildRunner(), **environment.options}
    return request.model_copy(update={"run_environment": replace(environment, options=options)})
