"""The ``slurm`` run environments edit in Docker; Slurm only decides where trusted work runs.

Both Slurm environments are driven through ``open_run_environment`` with a real
``DockerSandbox`` over ``FakeDockerEngine`` and a real host broker. A fake
program stands in for the cluster, so nothing patches the code under test.
"""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support.docker_daemon import DaemonBackend, daemon_docker_config, daemon_engine

from vibesys.run.environment import open_run_environment
from vs_project.api import RunResourceRequest
from vs_runtime.api.infrastructure import (
    RunEnvironment,
    RunEnvironmentRequest,
    SlurmEnvironment,
    SlurmGpuEnvironment,
    TrustedEvaluatorRequirements,
)
from vs_sandbox.api import DockerSandbox, SandboxKind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vs_runtime.api.infrastructure import RunEnvironmentSession

_SLURM_CONFIG = """[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/vibesys"

[slurm.transport]
kind = "ssh"
host = "test-cluster"

[vibesys]
remote_python = "/remote/venv/bin/python"
"""
_SLURM_GPU_CONFIG = """[slurm_gpu]
partitions = ["main"]
max_gpus = 8
max_time_minutes = 120
"""
# Stands in for ``vs_sandbox.slurm_command``: reports how it was called and fails the benchmark.
_FAKE_GATE = """\
import os, sys
print("gate", *sys.argv[1:], "cwd=" + os.getcwd())
sys.exit(7 if "benchmark" in sys.argv else 0)
"""


class _PassThroughConfinement:
    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        del workspace
        return list(argv)


def _environment(tmp_path: Path, name: str, backend: DaemonBackend) -> RunEnvironment:
    docker = daemon_docker_config(backend.engine)
    if name == "slurm":
        config = tmp_path / "slurm.toml"
        config.write_text(_SLURM_CONFIG, encoding="utf-8")
        gate = tmp_path / "fake_gate.py"
        gate.write_text(_FAKE_GATE, encoding="utf-8")
        return SlurmEnvironment(config, docker=docker, gate_wrapper=(sys.executable, str(gate)))
    config = tmp_path / "slurm-gpu.toml"
    config.write_text(_SLURM_GPU_CONFIG, encoding="utf-8")
    return SlurmGpuEnvironment(
        config,
        RunResourceRequest(accelerators_per_node=1, accelerator_backend="cuda"),
        docker=docker,
        job_confinement=_PassThroughConfinement(),
    )


def _request(
    tmp_path: Path,
    backend: DaemonBackend,
    *,
    accuracy: str | None = "python accuracy.py",
    benchmark: str | None = "python benchmark.py",
    framework_root: Path | None = None,
) -> RunEnvironmentRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=backend,
        agent_backend="stub",
        cli_provider=None,
        run_id="run-1",
        framework_root=framework_root or tmp_path / "framework",
        accuracy_command=accuracy,
        benchmark_command=benchmark,
        evaluator_requirements=TrustedEvaluatorRequirements(),
    )


def _open(
    tmp_path: Path,
    name: str,
    *,
    accuracy: str | None = "python accuracy.py",
    benchmark: str | None = "python benchmark.py",
) -> tuple[DaemonBackend, RunEnvironmentRequest, RunEnvironmentSession]:
    backend = DaemonBackend(daemon_engine(tmp_path))
    request = _request(tmp_path, backend, accuracy=accuracy, benchmark=benchmark)
    return backend, request, open_run_environment(_environment(tmp_path, name, backend), request)


@pytest.mark.parametrize("name", ["slurm", "slurm-gpu"])
def test_the_agent_never_runs_on_the_host_for_either_slurm_environment(
    tmp_path: Path, name: str
) -> None:
    backend, request, session = _open(tmp_path, name)
    try:
        assert isinstance(session.sandbox, DockerSandbox)
        assert session.view.cli_sandboxed
        assert backend.kinds == [SandboxKind.DOCKER]
        # No device nodes: whatever the agent computes on, it computes through the broker.
        assert backend.attach_accelerator == [False]
        run = next(call for call in backend.engine.calls if call[1] == "run")
        mounts = [run[i + 1] for i, flag in enumerate(run) if flag == "-v"]
        assert f"{request.workspace}:{request.workspace}" in mounts
        assert any(token.startswith("VIBESYS_COMMAND_BROKER_TOKEN=") for token in run)
    finally:
        session.close()


def test_a_slurm_gate_run_in_the_container_runs_on_the_host_through_the_wrapper(
    tmp_path: Path,
) -> None:
    _backend, request, session = _open(tmp_path, "slurm")
    try:
        accuracy = session.view.paths.accuracy_command
        benchmark = session.view.paths.benchmark_command
        assert accuracy is not None
        assert benchmark is not None

        passed = session.sandbox.execute(accuracy)
        failed = session.sandbox.execute(benchmark)

        plan = tmp_path / "logs" / "slurm-evaluation-plan.json"
        assert passed.exit_code == 0
        assert f"gate --plan {plan} accuracy cwd={request.workspace}" in passed.output
        # The gate's own exit status reaches the agent.
        assert failed.exit_code == 7
    finally:
        session.close()


def test_a_slurm_gate_the_task_does_not_plan_is_not_offered(tmp_path: Path) -> None:
    _backend, _, session = _open(tmp_path, "slurm", accuracy=None)
    try:
        assert session.view.paths.accuracy_command is None
        assert session.view.paths.benchmark_command is not None
    finally:
        session.close()


@pytest.mark.parametrize("name", ["slurm", "slurm-gpu"])
def test_closing_the_session_removes_every_host_socket(tmp_path: Path, name: str) -> None:
    backend, _, session = _open(tmp_path, name)
    run = next(call for call in backend.engine.calls if call[1] == "run")
    sockets = [
        Path(token.split("=", 1)[1])
        for token in run
        if token.startswith("VIBESYS_COMMAND_BROKER_SOCKET=")
    ]
    sockets += [
        Path(value)
        for key, value in session.view.profiler_mcp_env
        if key == "VIBESYS_SLURM_BROKER_SOCKET"
    ]
    assert len(sockets) == (2 if name == "slurm" else 1)
    assert all(socket.exists() for socket in sockets)

    session.close()

    assert not any(socket.exists() for socket in sockets)


# What the agent types to reach the host broker's gates, per environment.
_GATE_CLIENTS = {"slurm": "vibesys-gate", "slurm-gpu": "vibesys-gpu"}


@pytest.mark.parametrize("name", ["slurm", "slurm-gpu"])
def test_the_gate_client_is_mounted_where_the_agents_path_finds_it(
    tmp_path: Path, name: str
) -> None:
    """Regression for #1646: the client was reachable only by its absolute host path."""
    backend, _, session = _open(tmp_path, name)
    try:
        run = next(call for call in backend.engine.calls if call[1] == "run")
        mounts = [run[i + 1] for i, flag in enumerate(run) if flag == "-v"]
        client = _GATE_CLIENTS[name]
        [mount] = [mount for mount in mounts if mount.endswith(f":/usr/local/bin/{client}:ro")]
        launcher = Path(mount.split(":", 1)[0])
        assert launcher.name == client
        assert launcher.is_file()
        # The gate commands the run hands out name that same program.
        gate = session.view.paths.accuracy_command
        assert gate is not None
        assert shlex.split(gate)[0] == str(launcher)
    finally:
        session.close()


@pytest.mark.parametrize("name", ["slurm", "slurm-gpu"])
@pytest.mark.parametrize("accuracy", [None, "python accuracy.py"])
@pytest.mark.parametrize("benchmark", [None, "python benchmark.py"])
def test_the_environment_notes_name_the_gate_client_and_exactly_the_planned_gates(
    tmp_path: Path, name: str, accuracy: str | None, benchmark: str | None
) -> None:
    """Regression for #1646: no prompt told the agent a gate client exists."""
    _backend, _, session = _open(tmp_path, name, accuracy=accuracy, benchmark=benchmark)
    try:
        notes = session.view.prompt_notes
        client = _GATE_CLIENTS[name]
        assert (f"`{client} --gate accuracy`" in notes) is (accuracy is not None)
        assert (f"`{client} --gate benchmark`" in notes) is (benchmark is not None)
        if name == "slurm" and accuracy is None and benchmark is None:
            assert client not in notes
        # The notes and the gate commands handed to the run agree on what is planned.
        assert (session.view.paths.accuracy_command is not None) is (accuracy is not None)
        assert (session.view.paths.benchmark_command is not None) is (benchmark is not None)
    finally:
        session.close()
