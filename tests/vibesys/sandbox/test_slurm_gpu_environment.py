"""The ``slurm-gpu`` run environment: a Docker editor whose GPU work runs as Slurm jobs.

The environment is driven through ``open_run_environment`` with a real
``DockerSandbox`` over ``FakeDockerEngine``, a real host broker, and a fake
``srun`` program, so nothing patches the code under test. The fake daemon runs
``docker exec`` programs locally, which makes the container-side client talk to
the real broker socket.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.run.environment import open_run_environment
from vs_project.api import RunResourceRequest
from vs_runtime.api.infrastructure import (
    DockerInDockerUnsupportedError,
    RunEnvironmentRequest,
    RunEnvironmentSpec,
    SlurmGpuEnvironment,
    TrustedEvaluatorRequirements,
)
from vs_runtime.api.testing import DaemonBackend, daemon_docker_config, daemon_engine
from vs_sandbox.api import DockerSandbox, SandboxKind

if TYPE_CHECKING:
    from collections.abc import Sequence


# Stands in for srun: records its argv, then runs what follows ``--``.
_FAKE_SRUN = """\
import os, sys
with open({log!r}, "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
command = sys.argv[sys.argv.index("--") + 1 :]
os.execvp(command[0], command)
"""
_ACCURACY = 'print("accuracy ok")\n'


class _PassThroughConfinement:
    """Job confinement that runs the command as given (the fake srun is the cluster)."""

    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        del workspace
        return list(argv)


def _spec(tmp_path: Path, srun_log: Path) -> RunEnvironmentSpec:
    fake = tmp_path / "fake_srun.py"
    fake.write_text(_FAKE_SRUN.format(log=str(srun_log)), encoding="utf-8")
    config = tmp_path / "slurm-gpu.toml"
    config.write_text(
        f"""[slurm_gpu]
partitions = ["main"]
max_gpus = 8
max_time_minutes = 120
gate_time_minutes = 40
srun_command = ["{sys.executable}", "{fake}"]
scancel_command = ["{sys.executable}", "{fake}"]
""",
        encoding="utf-8",
    )
    return RunEnvironmentSpec(
        "slurm-gpu",
        {"config_path": str(config)},
        RunResourceRequest(accelerators_per_node=2, accelerator_backend="cuda"),
    )


def _request(
    tmp_path: Path, backend: DaemonBackend, *, docker_in_docker: bool = False
) -> RunEnvironmentRequest:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    accuracy = tmp_path / "accuracy.py"
    accuracy.write_text(_ACCURACY, encoding="utf-8")
    return RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=backend,
        agent_backend="stub",
        cli_provider=None,
        run_id="run-1",
        framework_root=tmp_path / "framework",
        accuracy_command=f"{sys.executable} {accuracy}",
        evaluator_requirements=TrustedEvaluatorRequirements(),
        docker_in_docker=docker_in_docker,
    )


def _open(tmp_path: Path, *, docker_in_docker: bool = False):  # noqa: ANN202  # lint-waiver: LW-954391 [ANN202]; the session type is the environment's public Protocol, named only by inference here.
    engine = daemon_engine(tmp_path)
    backend = DaemonBackend(engine)
    srun_log = tmp_path / "srun.log"
    spec = _spec(tmp_path, srun_log)
    environment = SlurmGpuEnvironment(
        Path(str(spec.options["config_path"])),
        spec.resources,
        docker=daemon_docker_config(engine),
        job_confinement=_PassThroughConfinement(),
    )
    request = _request(tmp_path, backend, docker_in_docker=docker_in_docker)
    return engine, backend, srun_log, request, open_run_environment(environment, request)


def test_the_agent_runs_in_a_gpuless_same_path_container_with_the_broker_mounted(
    tmp_path: Path,
) -> None:
    engine, backend, _log, request, session = _open(tmp_path)
    try:
        assert backend.kinds == [SandboxKind.DOCKER]
        assert backend.attach_accelerator == [False]
        assert isinstance(session.sandbox, DockerSandbox)
        run = next(call for call in engine.calls if call[1] == "run")
        mounts = [run[i + 1] for i, flag in enumerate(run) if flag == "-v"]
        assert f"{request.workspace}:{request.workspace}" in mounts
        launcher = session.view.paths.accuracy_command
        assert launcher is not None
        assert launcher.endswith("--gate accuracy")
        assert "CUDA_VISIBLE_DEVICES=" in run
        assert any(token.startswith("VIBESYS_COMMAND_BROKER_SOCKET=") for token in run)
        assert session.view.env_kind == "slurm-gpu"
        assert session.view.parallel_candidate_obstacle is not None
    finally:
        session.close()


def test_a_gate_run_from_the_container_runs_the_planned_command_in_a_slurm_job(
    tmp_path: Path,
) -> None:
    _engine, _backend, srun_log, request, session = _open(tmp_path)
    try:
        command = session.view.paths.accuracy_command
        assert command is not None
        result = session.sandbox.execute(command)
        assert result.exit_code == 0, result.output
        assert "accuracy ok" in result.output
        recorded = srun_log.read_text(encoding="utf-8")
        assert "--gres=gpu:2" in recorded
        assert "--time=40" in recorded
    finally:
        session.close()
    assert request.workspace.exists()


def test_closing_the_session_stops_the_container_and_then_the_broker(tmp_path: Path) -> None:
    engine, _backend, _log, _request, session = _open(tmp_path)
    socket_path = next(
        token.split("=", 1)[1]
        for call in engine.calls
        if call[1] == "run"
        for token in call
        if token.startswith("VIBESYS_COMMAND_BROKER_SOCKET=")
    )
    assert Path(socket_path).exists()

    session.close()

    verbs = [call[1] for call in engine.calls]
    assert "stop" in verbs or "rm" in verbs
    assert not Path(socket_path).exists()


def test_docker_in_docker_is_rejected_naming_the_key(tmp_path: Path) -> None:
    with pytest.raises(DockerInDockerUnsupportedError, match="docker_in_docker"):
        _open(tmp_path, docker_in_docker=True)
