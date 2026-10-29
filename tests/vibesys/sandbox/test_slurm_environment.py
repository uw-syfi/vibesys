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
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    plan_of,
    resolved,
    write_project,
)

from vibesys.run.environment import open_run_environment
from vs_agent.api.testing import FakeDockerBuildRunner
from vs_project.api import RunResourceRequest
from vs_runtime.api.infrastructure import (
    DockerEnvironmentConfig,
    EnvironmentSystem,
    EvaluatorGoModule,
    RunEnvironment,
    RunEnvironmentRequest,
    RunEnvironmentSessionRole,
    RunEnvironmentSpec,
    SlurmEnvironment,
    TrustedEvaluatorRequirements,
    build_run_environment,
    evaluator_go_module_ready_path,
    run_environment_deprecation,
)
from vs_runtime.api.testing import (
    DaemonBackend,
    daemon_docker_config,
    daemon_engine,
    idle_environment_system,
)
from vs_sandbox.api import (
    CaptureExecutionIdentity,
    CaptureExecutionKind,
    ComputeBackendImpl,
    DockerSandbox,
    SandboxKind,
)
from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    SlurmPolicyError,
    load_slurm_operator_settings,
)
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api import (
    OsThreads,
    ProcessSpec,
    SubprocessPipedProcessLauncher,
    SubprocessProbe,
    UnixNetwork,
)
from vs_sim.api.testing import (
    NO_PROCESSES,
    FakeProcesses,
    ManualClock,
    ProcessScript,
    SimThreads,
)
from vs_slurm.api import (
    FakeConnector,
    WorkspaceRootProbeFailed,
    WorkspaceRootProbeProven,
    WorkspaceRootProbeQueued,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from vs_runtime.api.infrastructure import RunEnvironmentSession
    from vs_sim.api import Threads
    from vs_slurm.api import WorkspaceRootProbeReport
from vs_sim.api import DISCARD


class _RecoverableCaptureSession(Protocol):
    @property
    def capture_execution(self) -> CaptureExecutionIdentity | None: ...


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


class _ImmediateSleepThreads:
    """Keep this real-confinement tier concurrent without waiting on scheduler observation gaps."""

    def __init__(self, delegate: Threads) -> None:
        self._delegate = delegate

    def sleep(self, seconds: float) -> None:
        del seconds

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


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
    backend: ComputeBackendImpl,
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
        session_role=RunEnvironmentSessionRole.CHILD,
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
            # The scheduler answers the GPU launcher's partition-limit query.
            slurm_processes=FakeProcesses(
                lambda _spec: ProcessScript(stdout=b"main* 2:00:00\n"),
                pass_time=ManualClock().advance,
            ),
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
        # Profile attribution has its own role: agent_gpu routes it through the
        # shared Slurm launcher, while every other mode keeps the editor runner.
        assert (session.capture_commands is not session.sandbox) is has_broker
        if has_broker:
            identity = cast("_RecoverableCaptureSession", session).capture_execution
            assert identity is not None
            assert session.capture_commands.id == identity.execution_id
            assert identity.kind is CaptureExecutionKind.SLURM_GPU
        else:
            identity = cast("_RecoverableCaptureSession", session).capture_execution
            assert identity is None
    finally:
        session.close()


def test_slurm_prepares_a_go_only_evaluator_before_capturing_its_support_tree(
    tmp_path: Path,
) -> None:
    backend = FakeComputeBackend(threads=SimThreads())
    package_root = tmp_path / "evaluator-package"
    package_root.mkdir()
    cache_root = tmp_path / "operator-cache/go-modules/digest"
    module = EvaluatorGoModule(
        root=package_root,
        cache_root=cache_root,
        package_digest="sha256:" + "a" * 64,
    )
    requirements = TrustedEvaluatorRequirements(
        package_root=package_root,
        toolchains=frozenset({"go"}),
        go_module=module,
    )
    cache_root.mkdir(parents=True)
    evaluator_go_module_ready_path(module, cache_root).mkdir()
    request = replace(
        _request(tmp_path, backend),
        evaluator_requirements=requirements,
        system=idle_environment_system(),
    )
    config = tmp_path / "slurm-local.toml"
    config.write_text(_SLURM_LOCAL_CONFIG, encoding="utf-8")
    environment = SlurmEnvironment(
        load_slurm_operator_settings(config),
        docker=DockerEnvironmentConfig(build_runner=FakeDockerBuildRunner()),
    )

    session = open_run_environment(environment, request)
    try:
        assert [creation.kind for creation in backend.creations[:2]] == [
            SandboxKind.LOCAL,
            SandboxKind.DOCKER,
        ]
        assert backend.creations[0].lifecycle_hooks
        cluster = session.view.cluster_evaluation
        assert cluster is not None
        assert cluster.plan.support_paths[".vibesys-evaluator-go-modules"] == cache_root
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
        ran = session.sandbox.execute(
            f"{client} --gpus 3 --time 45 -- echo from-the-job", output=DISCARD
        )
        refused = session.sandbox.execute(f"{client} --gpus 5 -- echo too-many", output=DISCARD)
        too_long = session.sandbox.execute(f"{client} --time 91 -- echo too-long", output=DISCARD)

        assert ran.exit_code == 0, ran.output
        assert "from-the-job" in ran.output
        recorded = (tmp_path / "srun.log").read_text(encoding="utf-8")
        assert "--gres=gpu:3" in recorded
        assert "--time=45" in recorded
        # The job carries the run's own prefix, which the queue status counts it by.
        run_prefix = (
            AgentGpuConfig(partitions=("main",), max_gpus=4, max_time_minutes=90)
            .for_run("run-1")
            .job_name_prefix
        )
        assert f"--job-name={run_prefix}-" in recorded
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


def _local_probe_environment(
    tmp_path: Path, kind: str, monkeypatch: pytest.MonkeyPatch
) -> SlurmEnvironment:
    if kind == "local":
        config = tmp_path / "slurm.toml"
        config.write_text(
            f'''[slurm]
name = "test-cluster"
remote_workspace_root = "{tmp_path / "remote"}"

[slurm.transport]
kind = "local"
shell_command = ["fake-connector", "shell"]
rsync_command = ["fake-connector", "rsync"]
''',
            encoding="utf-8",
        )
        return SlurmEnvironment(load_slurm_operator_settings(config))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    config = tmp_path / "slurm-gpu.toml"
    config.write_text(
        """[slurm_gpu]
partitions = ["main"]
max_gpus = 8
max_time_minutes = 120
srun_command = ["fake-connector", "shell", "srun"]
scancel_command = ["fake-connector", "shell", "scancel"]
""",
        encoding="utf-8",
    )
    return SlurmEnvironment.from_slurm_gpu(
        config,
        RunResourceRequest(accelerators_per_node=1, accelerator_backend="cuda"),
    )


@pytest.mark.parametrize("kind", ["local", "alias"])
def test_local_slurm_root_prepares_once_without_run_owned_roots_and_children_never_prepare(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    connector = FakeConnector(tmp_path / "cluster")
    threads = SimThreads()
    reports: list[WorkspaceRootProbeReport] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    request = RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=FakeComputeBackend(threads=threads),
        agent_backend="stub",
        cli_provider=None,
        run_id="root-with-no-owned-roots",
        framework_root=tmp_path,
        clock=ManualClock(),
        system=replace(idle_environment_system(), threads=threads, slurm_processes=connector),
        workspace_root_probe=reports.append,
    )
    environment = _local_probe_environment(tmp_path, kind, monkeypatch)

    def outage_after_root_start(
        _request: dict[str, object],
        _answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        raise OSError

    def start_the_run() -> None:
        environment.prepare(request)
        environment.prepare(request)
        _until_the_probe_settles(threads, reports)
        connector.intercept(outage_after_root_start)
        for index in range(3):
            environment.prepare(
                replace(
                    request,
                    log_dir=tmp_path / f"child-{index}-logs",
                    session_role=RunEnvironmentSessionRole.CHILD,
                )
            )

    try:
        threads.run(start_the_run)
    finally:
        threads.close()

    assert connector.submitted_job_ids() == ("5000",)
    assert reports[-1] == WorkspaceRootProbeProven()


def _until_the_probe_settles(threads: SimThreads, reports: list[WorkspaceRootProbeReport]) -> None:
    """Wait, in virtual time, for the root's probe to report how it ended."""
    while not reports or isinstance(reports[-1], WorkspaceRootProbeQueued):
        threads.sleep(1.0)


def test_local_slurm_root_probe_diagnostic_is_closed_and_bounded(tmp_path: Path) -> None:
    marker = "secret-boundary-marker"
    connector = FakeConnector(tmp_path / "cluster")

    def fail_with_untrusted_detail(
        _request: dict[str, object],
        _answer: Callable[[dict[str, object]], dict[str, object]],
    ) -> dict[str, object]:
        raise OSError(marker * 10_000)

    connector.intercept(fail_with_untrusted_detail)
    threads = SimThreads()
    reports: list[WorkspaceRootProbeReport] = []
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "slurm.toml"
    config.write_text(
        f'''[slurm]
name = "test-cluster"
remote_workspace_root = "{tmp_path / "remote"}"

[slurm.transport]
kind = "local"
shell_command = ["fake-connector", "shell"]
rsync_command = ["fake-connector", "rsync"]
''',
        encoding="utf-8",
    )
    request = RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=FakeComputeBackend(threads=threads),
        agent_backend="stub",
        cli_provider=None,
        run_id="bounded-diagnostic",
        framework_root=tmp_path,
        clock=ManualClock(),
        system=replace(idle_environment_system(), threads=threads, slurm_processes=connector),
        workspace_root_probe=reports.append,
    )

    def start_the_run() -> None:
        SlurmEnvironment(load_slurm_operator_settings(config)).prepare(request)
        _until_the_probe_settles(threads, reports)

    try:
        threads.run(start_the_run)
    finally:
        threads.close()

    # The run started; the probe's failure reaches the operator, closed and bounded.
    (failed,) = reports
    assert isinstance(failed, WorkspaceRootProbeFailed)
    assert marker not in failed.failure
    assert len(failed.failure) < 200


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
    scheduler_actions: list[str] = []

    def settled_scheduler(spec: ProcessSpec) -> ProcessScript:
        shell_command = spec.argv[-1]
        if shell_command == "sinfo" or shell_command.startswith("sinfo "):
            return ProcessScript(stdout=b"main* 2:00:00\n")
        action = next(
            (
                name
                for name in ("scancel", "squeue")
                if shell_command == name or shell_command.startswith(f"{name} ")
            ),
            None,
        )
        assert action is not None, spec.argv
        scheduler_actions.append(action)
        return ProcessScript()

    scheduler = FakeProcesses(settled_scheduler, lambda _seconds: None)
    base_request = _request(tmp_path, backend)
    request = replace(
        base_request,
        system=replace(
            base_request.system,
            threads=cast("Threads", _ImmediateSleepThreads(base_request.system.threads)),
            slurm_processes=scheduler,
        ),
    )
    environment = _real_confinement_environment(tmp_path, backend, [tools] if exposed else [])
    session = open_run_environment(environment, request)
    try:
        client = str(tmp_path / "logs" / "slurm-gpu" / "vibesys-gpu")

        read = session.sandbox.execute(f"{client} -- cat {tools}/interpreter.txt", output=DISCARD)
        write = session.sandbox.execute(
            f"{client} -- sh -c 'echo x > {tools}/written.txt'", output=DISCARD
        )

        assert (read.exit_code == 0) is exposed, read.output
        assert ("operator interpreter" in read.output) is exposed
        assert write.exit_code != 0
        assert not (tools / "written.txt").exists()
        failed_commands = 1 if exposed else 2
        assert scheduler_actions == ["scancel", *("squeue",) * 6] * failed_commands
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

        result = session.sandbox.execute(f"{client} -- {venv}/bin/python", output=DISCARD)

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


def test_the_slurm_agent_container_is_built_from_the_task_image_id(tmp_path: Path) -> None:
    """Regression: the Slurm editor built its agent container on the backend's default image.

    The run selection's task image reaches the backend the run opens with, and
    the agent layer is built ``FROM`` it: from the immutable ID resolved at
    launch, never the tag, so a later launch of the same task that prunes the
    tag cannot break this run's next agent container build.
    """
    task_image = "vibesys-task-build:0123456789abcdef-" + "0" * 32
    image_id = "sha256:" + "d" * 64
    config = tmp_path / "slurm-local.toml"
    config.write_text(_SLURM_LOCAL_CONFIG, encoding="utf-8")
    runner = FakeDockerBuildRunner()
    environment = build_run_environment(
        RunEnvironmentSpec(
            name="slurm",
            options={"config_path": str(config), "image": task_image, "build_runner": runner},
            image_id=image_id,
        )
    )
    # As the run's resources do: the backend opens on the environment's backend image.
    backend = FakeComputeBackend(threads=SimThreads(), image=environment.backend_image)
    request = replace(_request(tmp_path, backend), system=idle_environment_system())

    session = open_run_environment(environment, request)
    session.close()

    (agent_build,) = [argv for argv, _, _ in runner.calls if argv[1] == "build"]
    assert f"BASE_IMAGE=vibesys-base:{image_id.removeprefix('sha256:')}" in agent_build
    assert ("docker", "tag", image_id, f"vibesys-base:{'d' * 64}") in [
        argv for argv, _, _ in runner.calls
    ]
    assert not any(task_image in " ".join(argv) for argv, _, _ in runner.calls)
