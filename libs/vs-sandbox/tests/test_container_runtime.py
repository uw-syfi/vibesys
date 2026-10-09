"""A Docker sandbox for a container-topology task, driven against a fake daemon.

Everything here goes through ``DockerSandbox`` and the public
``vs_sandbox.api`` surface, with ``FakeDockerEngine`` standing in for the
daemon: argv selection, runtime availability, the nested daemon's readiness,
the no-fallback rule, and same-path mounts.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_sandbox.api import (
    ContainerRuntimeUnavailableError,
    DockerSandbox,
    NestedDaemonError,
    workspace_container_root,
)
from vs_sandbox.api.testing import FakeDockerEngine

if TYPE_CHECKING:
    from collections.abc import Sequence

_IMAGE = "agent-image"
_SYSBOX_RUNTIMES = ("runc", "sysbox-runc")

_SEGMENT = st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789-_", min_size=1, max_size=8)
# Under a root no test host has, so a generated workspace is never a real directory.
_ABSOLUTE_DIRS = st.lists(_SEGMENT, min_size=1, max_size=4).map(
    lambda parts: Path("/vs-container-runtime-test", *parts)
)


def _engine(
    tmp_path: Path,
    *,
    runtimes: Sequence[str] = ("runc",),
    nested_daemons_start: bool = True,
) -> FakeDockerEngine:
    return FakeDockerEngine(
        tmp_path / "engine",
        agent_ids=(os.getuid(), os.getgid()),
        runtimes=runtimes,
        nested_daemons_start=nested_daemons_start,
    )


def _run_call(engine: FakeDockerEngine) -> tuple[str, ...]:
    runs = [call for call in engine.calls if call[1] == "run"]
    assert len(runs) == 1
    return runs[0]


def _flag_values(argv: tuple[str, ...], flag: str) -> list[str]:
    return [argv[index + 1] for index, token in enumerate(argv) if token == flag]


def _sysbox_sandbox(tmp_path: Path, engine: FakeDockerEngine) -> tuple[DockerSandbox, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=_IMAGE,
        docker=engine,
        docker_in_docker=True,
    )
    return sandbox, workspace


def test_sysbox_sandbox_runs_under_the_sysbox_runtime_with_its_own_daemon(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)

    sandbox.start()
    try:
        run = _run_call(engine)
        assert _flag_values(run, "--runtime") == ["sysbox-runc"]
        assert engine.runtime_of(sandbox.container_id) == "sysbox-runc"
        assert engine.nested_daemon_running(sandbox.container_id)
    finally:
        sandbox.stop()
    assert not engine.nested_daemon_running(sandbox.id)


def test_sysbox_sandbox_never_mounts_the_host_socket_or_runs_privileged(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)

    sandbox.start()
    sandbox.stop()

    run = _run_call(engine)
    assert "--privileged" not in run
    assert not any("docker.sock" in token for token in run)


def test_the_daemon_is_started_and_awaited_before_the_sandbox_is_ready(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)

    sandbox.start()
    sandbox.stop()

    root_execs = [call for call in engine.calls if call[1] == "exec" and "root" in call[:5]]
    detached_start = [call for call in root_execs if "-d" in call]
    assert len(detached_start) == 1
    assert "dockerd" in detached_start[0][-1]
    readiness = [call for call in root_execs if "-d" not in call and "docker info" in call[-3]]
    assert len(readiness) == 1
    assert engine.calls.index(detached_start[0]) < engine.calls.index(readiness[0])


def test_a_daemon_that_never_becomes_ready_fails_the_start_and_releases_the_container(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES, nested_daemons_start=False)
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)

    with pytest.raises(NestedDaemonError, match="not ready"):
        sandbox.start()

    with pytest.raises(RuntimeError, match="no running container"):
        _ = sandbox.container_id
    assert any(call[1] == "rm" for call in engine.calls)


def test_a_host_without_sysbox_fails_early_and_never_falls_back_to_the_socket(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, runtimes=("runc",))
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)

    with pytest.raises(ContainerRuntimeUnavailableError) as raised:
        sandbox.start()

    message = str(raised.value)
    assert "sysbox-runc" in message
    assert "runc" in message
    assert "docker_in_docker" in message
    assert [call[1] for call in engine.calls] == ["info"]


def test_sysbox_cannot_be_combined_with_accelerators(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="accelerators"):
        DockerSandbox(
            host_workspace=str(tmp_path),
            image=_IMAGE,
            gpus="all",
            docker_in_docker=True,
        )


def test_an_ordinary_sandbox_is_unchanged(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = DockerSandbox(host_workspace=str(workspace), image=_IMAGE, docker=engine)

    sandbox.start()
    sandbox.stop()

    run = _run_call(engine)
    assert f"{workspace}:/workspace" in _flag_values(run, "-v")
    assert _flag_values(run, "--workdir") == ["/workspace"]
    assert "--runtime" not in run
    assert [call[1] for call in engine.calls if call[1] == "info"] == []
    assert sandbox.agent_path(workspace / "src") == "/workspace/src"


def test_an_ordinary_sandbox_argv_and_metadata_are_exactly_the_historical_ones(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = DockerSandbox(host_workspace=str(workspace), image=_IMAGE, docker=engine)

    sandbox.start()
    sandbox.stop()

    run = _run_call(engine)
    assert run[:2] == ("docker", "run")
    assert run[2:5] == ("-d", "--name", run[4])
    assert run[5:7] == ("-v", f"{workspace}:/workspace")
    assert run[-5:] == ("--workdir", "/workspace", _IMAGE, "sleep", "infinity")
    metadata = json.loads((workspace / ".docker_metadata.json").read_text())
    assert set(metadata) == {
        "image", "gpus", "devices", "group_add", "entrypoint", "shm_size",
        "bind_mounts", "env", "symlink_commands",
    }  # fmt: skip


def test_a_gpu_reselection_leaves_a_sysbox_sandbox_and_its_daemon_alone(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)
    sandbox.start()
    container = sandbox.container_id

    sandbox.restart_with_gpus("device=1")

    assert sandbox.container_id == container
    assert len([call for call in engine.calls if call[1] == "run"]) == 1
    sandbox.stop()


def test_the_workspace_mounts_at_its_host_path(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox, workspace = _sysbox_sandbox(tmp_path, engine)

    sandbox.start()
    sandbox.stop()

    assert f"{workspace}:{workspace}" in _flag_values(_run_call(engine), "-v")
    assert _flag_values(_run_call(engine), "--workdir") == [str(workspace)]


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(workspace=_ABSOLUTE_DIRS, relative=st.lists(_SEGMENT, max_size=3))
def test_every_host_path_under_the_workspace_is_the_same_path_in_the_container(
    tmp_path: Path, workspace: Path, relative: list[str]
) -> None:
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=_IMAGE,
        docker=_engine(tmp_path, runtimes=_SYSBOX_RUNTIMES),
        docker_in_docker=True,
    )

    nested = workspace.joinpath(*relative)
    assert sandbox.agent_path(nested) == str(nested)
    assert workspace_container_root(str(workspace), docker_in_docker=True) == str(workspace)
    assert workspace_container_root(str(workspace), docker_in_docker=False) == "/workspace"


@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(workspace=_ABSOLUTE_DIRS, relative=st.lists(_SEGMENT, max_size=3))
def test_exec_runs_in_the_agent_cwd_at_the_host_path(
    tmp_path: Path, workspace: Path, relative: list[str]
) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=_IMAGE,
        docker=engine,
        docker_in_docker=True,
    )
    sandbox.start()
    try:
        cwd = workspace.joinpath(*relative)
        argv = sandbox.wrap(["true"], cwd)
        assert argv[argv.index("-w") + 1] == str(cwd)
        assert PurePosixPath(argv[argv.index("-w") + 1]).is_absolute()
    finally:
        sandbox.stop()
