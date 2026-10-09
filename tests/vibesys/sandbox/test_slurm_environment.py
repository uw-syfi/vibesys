"""The ``slurm`` run environments edit in Docker; Slurm only decides where trusted work runs.

Both Slurm environments are driven through ``open_run_environment`` with a real
``DockerSandbox`` over ``FakeDockerEngine`` and a real host broker. A fake
program stands in for the cluster, so nothing patches the code under test.
"""

from __future__ import annotations

import os
import subprocess
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
        benchmark_command="python benchmark.py",
        evaluator_requirements=TrustedEvaluatorRequirements(),
    )


def _open(
    tmp_path: Path, name: str, *, accuracy: str | None = "python accuracy.py"
) -> tuple[DaemonBackend, RunEnvironmentRequest, RunEnvironmentSession]:
    backend = DaemonBackend(daemon_engine(tmp_path))
    request = _request(tmp_path, backend, accuracy=accuracy)
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


# What an agent image is guaranteed to provide to the profiler server: the standard
# library and pydantic (its MCP library requires it).
_ALLOWED_THIRD_PARTY = frozenset(
    {"pydantic", "pydantic_core", "annotated_types", "typing_extensions", "typing_inspection"}
)
_IMPORT_PROBE = """\
import sys
import vs_sandbox.api.slurm, vs_slurm.api, vs_slurm.wiring
allowed = sys.stdlib_module_names | set(sys.argv[1:])
loaded = {name.split(".")[0] for name in sys.modules}
extra = sorted(name for name in loaded if name not in allowed and not name.startswith(("vs_", "_")))
print(",".join(extra))
"""


def test_the_profiler_server_can_import_the_slurm_adapter_in_a_plain_image(
    tmp_path: Path,
) -> None:
    """The container's Python has no VibeSys install; the mounted sources must be enough."""
    framework_root = Path(__file__).resolve().parents[3]
    backend = DaemonBackend(daemon_engine(tmp_path))
    request = _request(tmp_path, backend, framework_root=framework_root)
    session = open_run_environment(_environment(tmp_path, "slurm", backend), request)
    try:
        pythonpath = dict(session.view.profiler_mcp_env)["PYTHONPATH"]
    finally:
        session.close()

    probe = subprocess.run(  # noqa: S603  # lint-waiver: LW-954392 [S603]; run the test's own interpreter on a fixed probe script.
        [sys.executable, "-c", _IMPORT_PROBE, *_ALLOWED_THIRD_PARTY],
        env={**os.environ, "PYTHONPATH": pythonpath},
        capture_output=True,
        text=True,
        check=False,
    )

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == ""
