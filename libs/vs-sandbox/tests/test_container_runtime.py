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
    require_sysbox_runtime,
    sysbox_unavailable,
    workspace_container_root,
)
from vs_sandbox.api.testing import (
    FakeDockerEngine,
    ScriptedDockerCli,
    docker_missing,
    docker_result,
)
from vs_sim.api import ProbeResult
from vs_sim.api.testing import FakePipedProcessLauncher, ScriptedProbe, SimThreads, examples

if TYPE_CHECKING:
    from collections.abc import Sequence
from vs_sim.api import DISCARD

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
        launcher=FakePipedProcessLauncher(),
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
        threads=SimThreads(),
    )
    return sandbox, workspace


def test_sysbox_sandbox_runs_under_the_sysbox_runtime_with_its_own_daemon(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)

    sandbox.start(output=DISCARD)
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

    sandbox.start(output=DISCARD)
    sandbox.stop()

    run = _run_call(engine)
    assert "--privileged" not in run
    assert not any("docker.sock" in token for token in run)


def test_the_daemon_is_started_and_awaited_before_the_sandbox_is_ready(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)

    sandbox.start(output=DISCARD)
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
        sandbox.start(output=DISCARD)

    with pytest.raises(RuntimeError, match="no running container"):
        _ = sandbox.container_id
    assert any(call[1] == "rm" for call in engine.calls)


def test_a_host_without_sysbox_fails_early_and_never_falls_back_to_the_socket(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path, runtimes=("runc",))
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)

    with pytest.raises(ContainerRuntimeUnavailableError) as raised:
        sandbox.start(output=DISCARD)

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
            docker=ScriptedDockerCli(),
            threads=SimThreads(),
        )


def test_an_ordinary_sandbox_is_unchanged(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sandbox = DockerSandbox(
        host_workspace=str(workspace), image=_IMAGE, docker=engine, threads=SimThreads()
    )

    sandbox.start(output=DISCARD)
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
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=_IMAGE,
        docker=engine,
        threads=SimThreads(),
        log_path=tmp_path / "docker.log",
    )

    sandbox.start(output=DISCARD)
    sandbox.stop()

    run = _run_call(engine)
    assert run[:2] == ("docker", "run")
    assert run[2:5] == ("-d", "--name", run[4])
    assert run[5:7] == ("-v", f"{workspace}:/workspace")
    assert run[-5:] == ("--workdir", "/workspace", _IMAGE, "sleep", "infinity")
    assert "--init" in run  # PID 1 must handle SIGTERM so `docker stop` is prompt
    metadata = json.loads((tmp_path / "docker.metadata.json").read_text())
    assert set(metadata) == {
        "image", "gpus", "devices", "group_add", "entrypoint", "shm_size",
        "bind_mounts", "env", "symlink_commands",
    }  # fmt: skip


def test_a_gpu_reselection_leaves_a_sysbox_sandbox_and_its_daemon_alone(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox, _ = _sysbox_sandbox(tmp_path, engine)
    sandbox.start(output=DISCARD)
    container = sandbox.container_id

    sandbox.restart_with_gpus("device=1")

    assert sandbox.container_id == container
    assert len([call for call in engine.calls if call[1] == "run"]) == 1
    sandbox.stop()


def test_the_workspace_mounts_at_its_host_path(tmp_path: Path) -> None:
    engine = _engine(tmp_path, runtimes=_SYSBOX_RUNTIMES)
    sandbox, workspace = _sysbox_sandbox(tmp_path, engine)

    sandbox.start(output=DISCARD)
    sandbox.stop()

    assert f"{workspace}:{workspace}" in _flag_values(_run_call(engine), "-v")
    assert _flag_values(_run_call(engine), "--workdir") == [str(workspace)]


@settings(
    max_examples=examples(pr=5, nightly=40),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(workspace=_ABSOLUTE_DIRS, relative=st.lists(_SEGMENT, max_size=3))
def test_every_host_path_under_the_workspace_is_the_same_path_in_the_container(
    tmp_path: Path, workspace: Path, relative: list[str]
) -> None:
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=_IMAGE,
        docker=_engine(tmp_path, runtimes=_SYSBOX_RUNTIMES),
        docker_in_docker=True,
        threads=SimThreads(),
    )

    nested = workspace.joinpath(*relative)
    assert sandbox.agent_path(nested) == str(nested)
    assert workspace_container_root(str(workspace), same_path=True) == str(workspace)
    assert workspace_container_root(str(workspace), same_path=False) == "/workspace"


@settings(
    max_examples=examples(pr=5, nightly=40),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
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
        threads=SimThreads(),
    )
    sandbox.start(output=DISCARD)
    try:
        cwd = workspace.joinpath(*relative)
        argv = sandbox.wrap(["true"], cwd)
        assert argv[argv.index("-w") + 1] == str(cwd)
        assert PurePosixPath(argv[argv.index("-w") + 1]).is_absolute()
    finally:
        sandbox.stop()


@settings(
    max_examples=examples(pr=5, nightly=40),
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(workspace=_ABSOLUTE_DIRS, relative=st.lists(_SEGMENT, max_size=3))
def test_a_same_path_workspace_needs_no_container_runtime(
    tmp_path: Path, workspace: Path, relative: list[str]
) -> None:
    """The workspace is mounted at its host path on a plain runtime, with no Sysbox."""
    engine = _engine(tmp_path, runtimes=("runc",))
    sandbox = DockerSandbox(
        host_workspace=str(workspace),
        image=_IMAGE,
        docker=engine,
        same_path_workspace=True,
        threads=SimThreads(),
    )
    sandbox.start(output=DISCARD)
    try:
        nested = workspace.joinpath(*relative)
        run = _run_call(engine)
        assert f"{workspace}:{workspace}" in _flag_values(run, "-v")
        assert "--runtime" not in run
        assert sandbox.agent_path(nested) == str(nested)
        argv = sandbox.wrap(["true"], nested)
        assert argv[argv.index("-w") + 1] == str(nested)
        assert "info" not in [call[1] for call in engine.calls]
    finally:
        sandbox.stop()


_RUNTIME_LISTINGS = st.one_of(
    st.lists(
        st.sampled_from(["runc", "sysbox-runc", "nvidia", "kata"]), unique=True, max_size=4
    ).map(lambda names: json.dumps({name: {} for name in names})),
    st.sampled_from(["", "null", "[]", "junk", '{"runc": {}', "42"]),
)
_LISTING_REPLIES = st.tuples(
    st.sampled_from([0, 0, 1, 125]),
    _RUNTIME_LISTINGS,
    st.sampled_from(["", "Cannot connect to the Docker daemon"]),
)


@given(reply=_LISTING_REPLIES)
def test_preflight_asks_the_runs_own_sysbox_question_and_gets_its_answer(
    reply: tuple[int, str, str],
) -> None:
    """Regression: a host with no Sysbox passed preflight, then the run stopped after Start.

    The run raises from ``require_sysbox_runtime`` before its image build; the check made
    ahead of it (``sysbox_unavailable``) reads the same ``docker info`` listing and gives
    the run's message, for a reply the run accepts and one it refuses alike.
    """
    cli = ScriptedDockerCli()
    cli.always(docker_result(*reply))
    probe = ScriptedProbe(lambda _argv: ProbeResult(*reply))

    try:
        require_sysbox_runtime(cli)
    except ContainerRuntimeUnavailableError as error:
        runs_answer: str | None = str(error)
    else:
        runs_answer = None

    assert sysbox_unavailable(probe) == runs_answer
    assert probe.calls[0][0] == cli.argvs[0]


def test_a_docker_that_cannot_answer_leaves_the_runtime_listing_unreadable() -> None:
    cli = ScriptedDockerCli()
    cli.always(docker_missing())
    probe = ScriptedProbe(lambda _argv: None)

    with pytest.raises(ContainerRuntimeUnavailableError) as raised:
        require_sysbox_runtime(cli)

    reason = sysbox_unavailable(probe)
    assert reason is not None
    assert "Could not list the Docker runtimes" in reason
    assert "Could not list the Docker runtimes" in str(raised.value)
