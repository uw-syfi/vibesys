"""The ``slurm`` run environments edit in Docker; Slurm only decides where trusted work runs.

Both Slurm environments are driven through ``open_run_environment`` with a real
``DockerSandbox`` over ``FakeDockerEngine`` and a real host broker. A fake
program stands in for the cluster, so nothing patches the code under test.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    plan_of,
    resolved,
    write_project,
)

from vibesys.run.environment import open_run_environment
from vs_project.api import RunResourceRequest
from vs_runtime.api.infrastructure import (
    EnvironmentSystem,
    RunEnvironment,
    RunEnvironmentRequest,
    SlurmEnvironment,
    TrustedEvaluatorRequirements,
    run_environment_deprecation,
)
from vs_runtime.api.testing import (
    DaemonBackend,
    daemon_docker_config,
    daemon_engine,
)
from vs_sandbox.api import DockerSandbox, SandboxKind
from vs_sandbox.api.slurm import SlurmPolicyError, load_slurm_operator_settings
from vs_sim.api import OsThreads, SubprocessPipedProcessLauncher, SubprocessProbe, UnixNetwork
from vs_sim.api.testing import NO_PROCESSES, ManualClock

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
shell_command = [{srun}]

[vibesys.agent_gpu]
partitions = ["main"]
max_gpus = 4
max_time_minutes = 90
"""
# Stands in for the Slurm shell (the local transport's ``bash -c``): it receives the
# ``srun`` command line as one argument, records the arguments, then runs what follows ``--``.
_FAKE_SRUN = """\
import os, shlex, sys
sys.argv = [sys.argv[0], *shlex.split(sys.argv[1])[1:]]
with open({log!r}, "a") as log:
    log.write(" ".join(sys.argv[1:]) + "\\n")
command = sys.argv[sys.argv.index("--") + 1 :]
os.execvp(command[0], command)
"""
# The local transport without the agent's GPU commands: no host command for the agent, and no
# remote capture.
_SLURM_LOCAL_CONFIG = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"
"""
_SLURM_GPU_CONFIG = """[slurm_gpu]
partitions = ["main"]
max_gpus = 8
max_time_minutes = 120
"""


def _bwrap_works() -> bool:
    """Whether bubblewrap can create the namespaces the host job confinement needs here."""
    bwrap = shutil.which("bwrap")
    if bwrap is None or not sys.platform.startswith("linux"):
        return False
    probe = SubprocessProbe().run(
        [bwrap, "--ro-bind", "/", "/", "--unshare-user", "--", "/bin/true"], timeout_seconds=10
    )
    return probe is not None and probe.returncode == 0


requires_bwrap = pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap")

# The interpreter that runs the fake programs standing in for the cluster.
_PYTHON = sys.executable
_ENVIRONMENTS = ["slurm", "slurm-local", "slurm-gpu", "slurm+gpu"]


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
        return SlurmEnvironment(
            load_slurm_operator_settings(
                _write_agent_gpu_slurm_config(tmp_path, tmp_path / "srun.log")
            ),
            docker=docker,
            job_confinement=_PassThroughConfinement(),
        )
    if name in ("slurm", "slurm-local"):
        config = tmp_path / "slurm.toml"
        config.write_text(
            _SLURM_CONFIG if name == "slurm" else _SLURM_LOCAL_CONFIG, encoding="utf-8"
        )
        return SlurmEnvironment(
            load_slurm_operator_settings(config),
            docker=docker,
        )
    config = tmp_path / "slurm-gpu.toml"
    config.write_text(_SLURM_GPU_CONFIG, encoding="utf-8")
    return SlurmEnvironment.from_slurm_gpu(
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
        clock=ManualClock(),
        system=EnvironmentSystem(
            threads=OsThreads(),
            network=UnixNetwork(),
            processes=NO_PROCESSES,
            launcher=SubprocessPipedProcessLauncher(),
            slurm_processes=NO_PROCESSES,
        ),
    )


def _open(
    tmp_path: Path,
    name: str,
    *,
    accuracy: str | None = "python accuracy.py",
    benchmark: str | None = "python benchmark.py",
) -> tuple[DaemonBackend, RunEnvironmentRequest, RunEnvironmentSession]:
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
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
        # Only an agent that may run GPU commands reaches a broker at all.
        has_broker = any(token.startswith("VIBESYS_COMMAND_BROKER_TOKEN=") for token in run)
        assert has_broker is (name not in ("slurm", "slurm-local"))
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
        has_gpu = name not in ("slurm", "slurm-local")
        assert any(token.startswith("VIBESYS_GPU=") for token in run) is has_gpu
        assert ("CUDA_VISIBLE_DEVICES=" in run) is has_gpu
        assert any(mount.endswith(":/usr/local/bin/vibesys-gpu:ro") for mount in mounts) is has_gpu
        assert ("GPU process runs as a Slurm job" in session.view.prompt_notes) is has_gpu
    finally:
        session.close()


def test_a_gpu_command_from_the_container_runs_in_a_slurm_job_within_the_operator_limits(
    tmp_path: Path,
) -> None:
    _backend, _, session = _open(tmp_path, "slurm+gpu")
    try:
        client = str(tmp_path / "logs" / "slurm-gpu" / "vibesys-gpu")
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


def test_the_profiler_follows_the_capability_not_the_environment_name(tmp_path: Path) -> None:
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
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
            assert session.view.editor_has_gpu is False
            assert (tmp_path / "logs" / "slurm-capture-plan.json").exists() is remote
        finally:
            session.close()
        (tmp_path / "logs" / "slurm-capture-plan.json").unlink(missing_ok=True)


def test_connector_transport_offers_the_remote_profiler_capability(tmp_path: Path) -> None:
    """Regression: connector runs offered no ROCprof capability at composition time."""
    config = tmp_path / "slurm-connector.toml"
    config.write_text(
        """[slurm]
name = "test-cluster"
remote_workspace_root = "/remote/vibesys"

[slurm.transport]
kind = "connector"
command = ["site-slurm-connector"]

[vibesys]
remote_python = "/remote/venv/bin/python"
""",
        encoding="utf-8",
    )
    environment = SlurmEnvironment(load_slurm_operator_settings(config))

    assert environment.default_profiler_id == "rocprof"
    assert environment.supported_profiler_ids == frozenset({"auto", "none", "rocprof"})


@pytest.mark.parametrize("transport", ["ssh", "connector"])
def test_agent_gpu_commands_are_rejected_without_the_local_transport(
    tmp_path: Path, transport: str
) -> None:
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
    config = _write_agent_gpu_slurm_config(tmp_path, tmp_path / "srun.log")
    replacement = {
        "ssh": 'kind = "ssh"\nhost = "login"',
        "connector": 'kind = "connector"\ncommand = ["c"]',
    }[transport]
    text = config.read_text(encoding="utf-8")
    shell = next(line for line in text.splitlines() if line.startswith("shell_command"))
    text = text.replace(f'kind = "local"\n{shell}', replacement)
    config.write_text(text, encoding="utf-8")
    del backend

    with pytest.raises(SlurmPolicyError, match=rf"vibesys\.agent_gpu.*{transport}"):
        load_slurm_operator_settings(config)


def test_the_slurm_gpu_alias_deprecation_is_reported_once_per_run_not_once_per_open(
    tmp_path: Path,
) -> None:
    """Regression: a run opens its environment for every candidate, and each open logged it."""
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
    logged: list[str] = []
    request = replace(_request(tmp_path, backend), log=logged.append)
    environment = _environment(tmp_path, "slurm-gpu", backend)

    for _ in range(3):
        open_run_environment(environment, request).close()

    assert [line for line in logged if "deprecated" in line] == []
    warning = run_environment_deprecation(environment)
    assert warning is not None
    assert "--run-environment slurm" in warning
    assert "[vibesys.agent_gpu]" in warning
    assert str(Path("~/.cache/vibesys/slurm-gpu-stage").expanduser()) in warning


@pytest.mark.parametrize("name", ["slurm", "slurm-local", "slurm+gpu"])
def test_a_selection_that_is_not_deprecated_has_no_deprecation(tmp_path: Path, name: str) -> None:
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )

    assert run_environment_deprecation(_environment(tmp_path, name, backend)) is None


def _read_only_tools(tmp_path: Path) -> Path:
    tools = tmp_path / "operator-tools"
    tools.mkdir()
    (tools / "interpreter.txt").write_text("operator interpreter\n", encoding="utf-8")
    return tools


def _real_confinement_environment(
    tmp_path: Path, backend: DaemonBackend, read_only_paths: Sequence[Path]
) -> SlurmEnvironment:
    """The agent GPU environment with the host's real job confinement, not a pass-through."""
    config = _write_agent_gpu_slurm_config(tmp_path, tmp_path / "srun.log")
    paths = ", ".join(f'"{path}"' for path in read_only_paths)
    with config.open("a", encoding="utf-8") as handle:
        handle.write(f"read_only_paths = [{paths}]\n")
    return SlurmEnvironment(
        load_slurm_operator_settings(config),
        docker=daemon_docker_config(backend.engine),
    )


@requires_bwrap
@pytest.mark.parametrize("exposed", [True, False])
def test_a_gpu_job_reads_an_operator_declared_path_outside_the_workspace_and_never_writes_it(
    tmp_path: Path, *, exposed: bool
) -> None:
    """The job's confinement hides everything but the workspace unless the operator exposes it."""
    tools = _read_only_tools(tmp_path)
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
    request = _request(tmp_path, backend)
    environment = _real_confinement_environment(tmp_path, backend, [tools] if exposed else [])
    session = open_run_environment(environment, request)
    try:
        client = str(tmp_path / "logs" / "slurm-gpu" / "vibesys-gpu")

        read = session.sandbox.execute(f"{client} -- cat {tools}/interpreter.txt")
        write = session.sandbox.execute(f"{client} -- sh -c 'echo x > {tools}/written.txt'")

        assert (read.exit_code == 0) is exposed, read.output
        assert ("operator interpreter" in read.output) is exposed
        assert write.exit_code != 0
        assert not (tools / "written.txt").exists()
    finally:
        session.close()


def test_a_read_only_path_that_does_not_exist_fails_the_open_naming_the_key(
    tmp_path: Path,
) -> None:
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
    environment = _real_confinement_environment(tmp_path, backend, [tmp_path / "missing-venv"])

    with pytest.raises(ValueError, match=r"agent_gpu\.read_only_paths.*missing-venv"):
        open_run_environment(environment, _request(tmp_path, backend))


def _aliased_venv(tmp_path: Path) -> tuple[Path, Path]:
    """A venv whose interpreter is reached through an alias directory, as uv lays it out."""
    real = tmp_path / "store" / "cpython-3.12.15"
    (real / "bin").mkdir(parents=True)
    interpreter = real / "bin" / "python3.12"
    interpreter.write_text("#!/bin/sh\necho interpreter ran\n", encoding="utf-8")
    interpreter.chmod(0o755)
    alias = tmp_path / "store" / "cpython-3.12"
    alias.symlink_to(real.name)
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").symlink_to(alias / "bin" / "python3.12")
    return tmp_path / "venv", alias


def test_a_venv_whose_interpreter_link_leaves_the_listed_paths_fails_the_open_naming_what_to_add(
    tmp_path: Path,
) -> None:
    venv, alias = _aliased_venv(tmp_path)
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
    environment = _real_confinement_environment(tmp_path, backend, [venv])

    with pytest.raises(ValueError, match=r"agent_gpu\.read_only_paths") as raised:
        open_run_environment(environment, _request(tmp_path, backend))

    assert f"{venv / 'bin' / 'python'} -> {alias / 'bin' / 'python3.12'}" in str(raised.value)
    assert f"add {alias / 'bin' / 'python3.12'}" in str(raised.value)


@requires_bwrap
def test_a_gpu_job_runs_an_interpreter_reached_through_a_link_chain_once_the_alias_is_listed(
    tmp_path: Path,
) -> None:
    venv, alias = _aliased_venv(tmp_path)
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
    environment = _real_confinement_environment(tmp_path, backend, [venv, alias])
    session = open_run_environment(environment, _request(tmp_path, backend))
    try:
        client = str(tmp_path / "logs" / "slurm-gpu" / "vibesys-gpu")

        result = session.sandbox.execute(f"{client} -- {venv}/bin/python")

        assert result.exit_code == 0, result.output
        assert "interpreter ran" in result.output
    finally:
        session.close()


@pytest.mark.parametrize("paths", [(), ("/opt/venv",), ("/opt/venv", "/home/op/data")])
def test_the_notes_tell_the_agent_gpu_commands_run_in_the_hosts_environment_and_list_the_paths(
    tmp_path: Path, paths: tuple[str, ...]
) -> None:
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
    config = _write_agent_gpu_slurm_config(tmp_path, tmp_path / "srun.log")
    quoted = ", ".join(f'"{path}"' for path in paths)
    with config.open("a", encoding="utf-8") as handle:
        handle.write(f"read_only_paths = [{quoted}]\n")
    environment = SlurmEnvironment(
        load_slurm_operator_settings(config),
        docker=daemon_docker_config(backend.engine),
        job_confinement=_PassThroughConfinement(),
    )
    session = open_run_environment(environment, _request(tmp_path, backend))
    try:
        notes = session.view.prompt_notes
        assert "host's environment, not in this container" in notes
        for path in paths:
            assert f"`{path}`" in notes
        assert ("these read-only paths" in notes) is bool(paths)
        assert ("exposed no extra paths" in notes) is not paths
    finally:
        session.close()


# What each configuration lets a run select: the remote capture needs the SSH transport's
# profiler broker, the agent's own commands lift the restriction, and the rest offers none.
_PROFILERS = {
    "slurm": ("rocprof", frozenset({"auto", "none", "rocprof"})),
    "slurm-local": ("none", frozenset({"auto", "none"})),
    "slurm-gpu": ("nsys", None),
    "slurm+gpu": ("nsys", None),
}


@pytest.mark.parametrize("name", _ENVIRONMENTS)
def test_the_profilers_a_run_may_select_follow_how_the_environment_can_run_one(
    tmp_path: Path, name: str
) -> None:
    backend = DaemonBackend(
        daemon_engine(tmp_path, launcher=SubprocessPipedProcessLauncher()), threads=OsThreads()
    )
    environment = _environment(tmp_path, name, backend)

    default, supported = _PROFILERS[name]
    assert environment.default_profiler_id == default
    assert environment.supported_profiler_ids == supported


def test_without_a_remote_profiler_transport_the_agent_is_offered_no_remote_capture(
    tmp_path: Path,
) -> None:
    _backend, _request, session = _open(tmp_path, "slurm-local")
    try:
        assert session.view.profile_execution == "local"
        assert session.view.profiler_mcp_env == ()
        assert session.view.profiler_mcp_resources == ()
    finally:
        session.close()


@pytest.mark.parametrize("name", _ENVIRONMENTS)
def test_the_dynamic_loop_accepts_every_slurm_configuration_including_agent_gpu_commands(
    tmp_path: Path, name: str
) -> None:
    """Regression: agent GPU commands carried a fixed blocker that refused the dynamic loop."""
    _backend, _request, session = _open(tmp_path, name)
    try:
        assert session.view.parallel_candidate_obstacle is None
        project = tmp_path / "project"
        write_project(project)
        plan_of(resolved(project, environment=session.view))
    finally:
        session.close()


# Agents request trusted evaluation through the evaluation tool: the shell offers them no gate.


@pytest.mark.parametrize("name", _ENVIRONMENTS)
@pytest.mark.parametrize("accuracy", [None, "python accuracy.py"])
def test_agents_are_given_no_gate_command_or_gate_text(
    tmp_path: Path, name: str, accuracy: str | None
) -> None:
    backend, _, session = _open(tmp_path, name, accuracy=accuracy)
    try:
        run = _run_tokens(backend)
        mounts = [run[i + 1] for i, flag in enumerate(run) if flag == "-v"]
        has_gpu = name not in ("slurm", "slurm-local")
        assert session.view.paths.accuracy_command is None
        assert session.view.paths.benchmark_command is None
        assert "--gate" not in session.view.prompt_notes
        assert "vibesys-gate" not in session.view.prompt_notes
        assert not any(mount.endswith(":/usr/local/bin/vibesys-gate:ro") for mount in mounts)
        assert any(mount.endswith(":/usr/local/bin/vibesys-gpu:ro") for mount in mounts) is has_gpu
        # Without agent GPU commands the container reaches no broker at all.
        has_broker = any(token.startswith("VIBESYS_COMMAND_BROKER_SOCKET=") for token in run)
        assert has_broker is has_gpu
    finally:
        session.close()


@pytest.mark.parametrize("name", _ENVIRONMENTS)
def test_closing_the_session_removes_every_host_socket(tmp_path: Path, name: str) -> None:
    backend, _, session = _open(tmp_path, name)
    run = _run_tokens(backend)
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
    assert all(socket.exists() for socket in sockets)

    session.close()

    assert not any(socket.exists() for socket in sockets)
