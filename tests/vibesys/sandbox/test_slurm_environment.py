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

from vibesys.run.environment import open_run_environment
from vs_project.api import RunResourceRequest
from vs_runtime.api.infrastructure import (
    RunEnvironment,
    RunEnvironmentRequest,
    SlurmEnvironment,
    SlurmGpuEnvironment,
    TrustedEvaluatorRequirements,
)
from vs_runtime.api.testing import DaemonBackend, daemon_docker_config, daemon_engine
from vs_sandbox.api import DockerSandbox, SandboxKind
from vs_sandbox.api.slurm import SlurmPolicyError

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
# The slurm environment with the agent's own GPU commands: local transport only.
_SLURM_AGENT_GPU_CONFIG = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"

[vibesys.agent_gpu]
partitions = ["main"]
max_gpus = 4
max_time_minutes = 90
srun_command = [{srun}]
scancel_command = [{srun}]
"""
# Stands in for srun: records its argv, then runs what follows ``--``.
_FAKE_SRUN = """\
import os, sys
with open({log!r}, "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
command = sys.argv[sys.argv.index("--") + 1 :]
os.execvp(command[0], command)
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


# The interpreter that runs the fake programs standing in for the cluster.
_PYTHON = sys.executable
_ENVIRONMENTS = ["slurm", "slurm-gpu", "slurm+gpu"]


class _PassThroughConfinement:
    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        del workspace
        return list(argv)


def _write_agent_gpu_slurm_config(tmp_path: Path, srun_log: Path) -> Path:
    fake = tmp_path / "fake_srun.py"
    fake.write_text(_FAKE_SRUN.format(log=str(srun_log)), encoding="utf-8")
    config = tmp_path / "slurm-agent-gpu.toml"
    program = f'"{_PYTHON}", "{fake}"'
    config.write_text(_SLURM_AGENT_GPU_CONFIG.format(srun=program), encoding="utf-8")
    return config


def _environment(tmp_path: Path, name: str, backend: DaemonBackend) -> RunEnvironment:
    docker = daemon_docker_config(backend.engine)
    if name == "slurm+gpu":
        gate = tmp_path / "fake_gate.py"
        gate.write_text(_FAKE_GATE, encoding="utf-8")
        return SlurmEnvironment(
            _write_agent_gpu_slurm_config(tmp_path, tmp_path / "srun.log"),
            docker=docker,
            gate_wrapper=(_PYTHON, str(gate)),
            job_confinement=_PassThroughConfinement(),
        )
    if name == "slurm":
        config = tmp_path / "slurm.toml"
        config.write_text(_SLURM_CONFIG, encoding="utf-8")
        gate = tmp_path / "fake_gate.py"
        gate.write_text(_FAKE_GATE, encoding="utf-8")
        return SlurmEnvironment(config, docker=docker, gate_wrapper=(_PYTHON, str(gate)))
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


@pytest.mark.parametrize("name", _ENVIRONMENTS)
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


@pytest.mark.parametrize("name", _ENVIRONMENTS)
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
_GATE_CLIENTS = {"slurm": "vibesys-gate", "slurm-gpu": "vibesys-gpu", "slurm+gpu": "vibesys-gpu"}


@pytest.mark.parametrize("name", _ENVIRONMENTS)
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


@pytest.mark.parametrize("name", _ENVIRONMENTS)
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


def _run_tokens(backend: DaemonBackend) -> list[str]:
    return list(next(call for call in backend.engine.calls if call[1] == "run"))


@pytest.mark.parametrize("name", _ENVIRONMENTS)
def test_the_container_gets_the_gpu_launcher_exactly_when_the_agent_may_run_gpu_commands(
    tmp_path: Path, name: str
) -> None:
    backend, _, session = _open(tmp_path, name)
    try:
        run = _run_tokens(backend)
        mounts = [run[i + 1] for i, flag in enumerate(run) if flag == "-v"]
        has_gpu = name != "slurm"
        assert any(token.startswith("VIBESYS_GPU=") for token in run) is has_gpu
        assert ("CUDA_VISIBLE_DEVICES=" in run) is has_gpu
        assert any(mount.endswith(":/usr/local/bin/vibesys-gpu:ro") for mount in mounts) is has_gpu
        assert any(mount.endswith(":/usr/local/bin/vibesys-gate:ro") for mount in mounts) is (
            not has_gpu
        )
        assert ("GPU process runs as a Slurm job" in session.view.prompt_notes) is has_gpu
    finally:
        session.close()


def test_a_gpu_command_from_the_container_runs_in_a_slurm_job_within_the_operator_limits(
    tmp_path: Path,
) -> None:
    _backend, _, session = _open(tmp_path, "slurm+gpu")
    try:
        gate = session.view.paths.accuracy_command
        assert gate is not None
        # The fake daemon runs programs on the host, where the bare name is not on PATH.
        client = shlex.split(gate)[0]
        ran = session.sandbox.execute(f"{client} --gpus 3 --time 45 -- echo from-the-job")
        refused = session.sandbox.execute(f"{client} --gpus 5 -- echo too-many")
        too_long = session.sandbox.execute(f"{client} --time 91 -- echo too-long")

        assert ran.exit_code == 0, ran.output
        assert "from-the-job" in ran.output
        recorded = (tmp_path / "srun.log").read_text(encoding="utf-8")
        assert "--gres=gpu:3" in recorded
        assert "--time=45" in recorded
        # A request above the operator's limits never reaches srun.
        assert "too-many" not in recorded
        assert "too-long" not in recorded
        assert refused.exit_code != 0
        assert "limit of 4" in refused.output
        assert too_long.exit_code != 0
        assert "limit of 90" in too_long.output
    finally:
        session.close()


def test_the_gates_still_run_through_the_sbatch_wrapper_when_the_agent_may_run_gpu_commands(
    tmp_path: Path,
) -> None:
    _backend, request, session = _open(tmp_path, "slurm+gpu")
    try:
        accuracy = session.view.paths.accuracy_command
        assert accuracy is not None
        passed = session.sandbox.execute(accuracy)
        plan = tmp_path / "logs" / "slurm-evaluation-plan.json"
        assert f"gate --plan {plan} accuracy cwd={request.workspace}" in passed.output
        assert not (tmp_path / "srun.log").exists()
    finally:
        session.close()


def test_the_profiler_follows_the_capability_not_the_environment_name(tmp_path: Path) -> None:
    backend = DaemonBackend(daemon_engine(tmp_path))
    plain = _environment(tmp_path, "slurm", backend)
    gpu = _environment(tmp_path, "slurm+gpu", backend)

    assert (plain.default_profiler_id, plain.supported_profiler_ids) == (
        "rocprof",
        frozenset({"auto", "none", "rocprof"}),
    )
    assert plain.requires_local_profiler_preflight is False
    assert (gpu.default_profiler_id, gpu.supported_profiler_ids) == ("nsys", None)
    assert gpu.requires_local_profiler_preflight is True

    request = _request(tmp_path, backend)
    for name, remote in (("slurm", True), ("slurm+gpu", False)):
        session = open_run_environment(_environment(tmp_path, name, backend), request)
        try:
            assert (session.view.profile_execution == "remote") is remote
            assert bool(session.view.profiler_mcp_env) is remote
            assert (tmp_path / "logs" / "slurm-capture-plan.json").exists() is remote
        finally:
            session.close()
        (tmp_path / "logs" / "slurm-capture-plan.json").unlink(missing_ok=True)


@pytest.mark.parametrize("transport", ["ssh", "connector"])
def test_agent_gpu_commands_are_rejected_without_the_local_transport(
    tmp_path: Path, transport: str
) -> None:
    backend = DaemonBackend(daemon_engine(tmp_path))
    config = _write_agent_gpu_slurm_config(tmp_path, tmp_path / "srun.log")
    replacement = {
        "ssh": 'kind = "ssh"\nhost = "login"',
        "connector": 'kind = "connector"\ncommand = ["c"]',
    }[transport]
    text = config.read_text(encoding="utf-8").replace('kind = "local"', replacement)
    config.write_text(text, encoding="utf-8")
    environment = SlurmEnvironment(config, docker=daemon_docker_config(backend.engine))

    with pytest.raises(SlurmPolicyError, match=rf"vibesys\.agent_gpu.*{transport}"):
        open_run_environment(environment, _request(tmp_path, backend))
