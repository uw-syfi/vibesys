"""Public run-control metadata writers never publish partial documents."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api import DockerSandbox
from vs_sandbox.api.slurm import (
    SlurmCapturePlan,
    SlurmCapturePlanError,
    read_slurm_capture_plan,
    write_slurm_capture_plan,
)
from vs_sandbox.api.testing import ScriptedDockerCli, docker_result
from vs_sim.api.testing import SimThreads, file_size_limit

if TYPE_CHECKING:
    from pathlib import Path
from vs_sim.api import DISCARD


def test_capture_plan_interrupted_at_every_byte_remains_readable(tmp_path: Path) -> None:
    path = tmp_path / "capture.json"
    old = SlurmCapturePlan(profile_command=("true",))
    new = SlurmCapturePlan(profile_command=("echo", "new command"))
    for interruption in range(len(new.model_dump_json().encode()) + 1):
        write_slurm_capture_plan(path, old)
        with file_size_limit(interruption), pytest.raises(OSError, match="File too large"):
            write_slurm_capture_plan(path, new)
        assert read_slurm_capture_plan(path) == old
        assert not list(path.parent.glob(".*.tmp"))


def test_a_capture_plan_that_is_not_a_regular_file_is_invalid_not_a_hang(tmp_path: Path) -> None:
    # Reading a FIFO blocks until a writer appears; the reader must refuse it instead.
    path = tmp_path / "capture.json"
    os.mkfifo(path)

    with pytest.raises(SlurmCapturePlanError, match="invalid Slurm capture plan"):
        read_slurm_capture_plan(path)


def test_docker_metadata_interrupted_at_every_byte_preserves_last_document(
    tmp_path: Path,
) -> None:
    docker = ScriptedDockerCli()
    docker.on(("docker", "run"), docker_result(stdout="test-container\n"))
    docker.on(
        ("docker", "exec", "test-container", "sh", "-c", "id -u agent && id -g agent"),
        docker_result(stdout=f"{os.getuid()}\n{os.getgid()}\n"),
    )
    docker.always(docker_result())
    sandbox = DockerSandbox(
        str(tmp_path / "workspace"),
        image="test-image",
        docker=docker,
        threads=SimThreads(),
        log_path=tmp_path / "logs" / "docker.log",
    )
    path = tmp_path / "logs" / "docker.metadata.json"
    sandbox.start(output=DISCARD)
    try:
        sandbox.save_symlink_commands(["old"])
        old = path.read_bytes()
        for interruption in range(len(old) + 1):
            with file_size_limit(interruption):
                sandbox.save_symlink_commands(["new-command"])
            assert path.read_bytes() == old
            assert not list(path.parent.glob(".*.tmp"))
    finally:
        sandbox.stop()
