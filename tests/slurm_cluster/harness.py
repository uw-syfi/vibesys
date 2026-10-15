"""Open the ``slurm`` run environment, in each of its configurations, against a real cluster.

The kinds are ``slurm`` (SSH transport), ``slurm-local`` (local transport),
``slurm-local-gpu`` (local transport plus the agent's own GPU commands), and
``slurm-gpu-alias`` (the deprecated ``slurm-gpu`` selection, translated to the
previous one).

The environments are opened through ``open_run_environment`` exactly as a run
opens them. What differs from production is only what the tier cannot have on a
single host: the agent image carries no agent CLI, and the host reaches Slurm
through the operator-configured command prefixes (``ssh`` for the SSH transport,
the login-node shim as the local transport's shell). The agent side is a real
Docker container; tests drive it with scripted commands through the session's sandbox.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import textwrap
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from tests.slurm_cluster.cluster import SlurmCluster, build_image, docker

from vibesys.run.environment import open_run_environment
from vs_project.api import RunResourceRequest
from vs_runtime.api.infrastructure import (
    DockerEnvironmentConfig,
    EnvironmentSystem,
    RunEnvironment,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    SlurmEnvironment,
    TrustedEvaluatorRequirements,
)
from vs_sandbox.api import ComputeBackend, DockerSandbox, LocalBackend, SubprocessDockerCli
from vs_sandbox.api.slurm import load_slurm_operator_settings
from vs_sim.api import (
    MonotonicClock,
    OsProcesses,
    OsThreads,
    SubprocessPipedProcessLauncher,
    UnixNetwork,
)

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from vs_sandbox.api import CommandResult
#: The label ``DockerSandbox`` puts on its containers (``vs_sandbox.docker_sandbox.RUN_ID_LABEL``).
_RUN_ID_LABEL = "vibesys.run-id"
BENCHMARK_OUTPUT_ARGUMENT = "--output"
#: The kinds in which the agent runs its own GPU commands.
GPU_KINDS = ("slurm-local-gpu", "slurm-gpu-alias")
ALL_KINDS = ("slurm", "slurm-local", "slurm-local-gpu", "slurm-gpu-alias")
#: The container's own ``/tmp``, where the framework's benchmark result is relayed to.
CONTAINER_TMP = "/tmp"  # noqa: S108  # lint-waiver: LW-960011 [S108]; the framework result location is fixed, and it is this directory in the agent container.
# > A tempfile call would name the test process's directory, not the container's.


def build_agent_image() -> str:
    """Build the agent test image; return its immutable image id."""
    tag = build_image(
        dockerfile="agent.Dockerfile", tag_prefix="vibesys-slurm-test-agent", build_args={}
    )
    return docker("image", "inspect", "--format", "{{.Id}}", tag).stdout.strip()


class PrebuiltImageRunner:
    """The agent image build step, answered with an image the tier built itself.

    The agent layer (CLIs and toolchains) is not under test and takes minutes to
    build, so the environment's ``docker build`` is replaced with this runner.
    Everything after it, the container, its mounts and its processes, is real.
    """

    def __init__(self, image_id: str) -> None:
        """Answer every build with success and every inspect with *image_id*."""
        self._image_id = image_id

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float,
        on_stderr_line: Callable[[str], None] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Return the scripted outcome for ``docker build`` or ``docker image inspect``."""
        del cwd, timeout, on_stderr_line
        stdout = self._image_id if argv[1] == "image" else ""
        return subprocess.CompletedProcess(tuple(argv), 0, stdout, "")


@dataclass(frozen=True)
class OpenRun:
    """One opened run: its session, its request, and the inputs that made them."""

    session: RunEnvironmentSession
    request: RunEnvironmentRequest
    run_id: str
    kind: str
    #: What the environment logged while opening, for the one-time notices.
    log_lines: tuple[str, ...] = ()

    def agent(
        self, command: str, *, cancel: threading.Event | None = None, timeout: int | None = None
    ) -> CommandResult:
        """Run a scripted *command* as the agent, in the container, and return its result."""
        return self.session.sandbox.execute(command, timeout=timeout, cancel=cancel)

    @property
    def has_gpu_commands(self) -> bool:
        """Whether the agent may run its own GPU commands (``$VIBESYS_GPU``) in this run."""
        return self.kind in GPU_KINDS

    @property
    def container_id(self) -> str:
        """The id of the agent container."""
        return cast("DockerSandbox", self.session.sandbox).container_id

    @property
    def workspace(self) -> Path:
        """The workspace, mounted in the agent container at this same path."""
        return self.request.workspace


def parse_env(output: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines (as ``env`` prints them) into a dictionary."""
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def in_background[T](work: Callable[[], T]) -> Future[T]:
    """Run *work* on its own thread; read the result with ``.result(timeout=...)``."""
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(work)
    pool.shutdown(wait=False)
    return future


def run_container_ids(run_id: str) -> list[str]:
    """Return ids of the containers labelled with *run_id* (running or not)."""
    return docker("ps", "-aq", "--filter", f"label={_RUN_ID_LABEL}={run_id}").stdout.split()


def remove_run_containers(run_id: str) -> None:
    """Force-remove the containers this test started (those carrying its unique run id)."""
    ids = run_container_ids(run_id)
    if ids:
        docker("rm", "-f", "-v", *ids, check=False)


_AGENT_GPU_TABLE = textwrap.dedent(
    """\

    [vibesys.agent_gpu]
    partitions = ["main"]
    max_gpus = 4
    max_time_minutes = 10
    default_time_minutes = 5
    read_only_paths = [{tools}]
    """
)
#: The one file in the operator's read-only tools directory (see :func:`operator_tools`).
OPERATOR_TOOL_FILE = "interpreter.txt"
OPERATOR_TOOL_TEXT = "operator interpreter\n"


def operator_tools(cluster: SlurmCluster) -> Path:
    """The directory of host tools outside any workspace that ``slurm-local-gpu`` exposes read-only.

    It stands for the operator's Python environment in a home directory: shared
    with the node, and invisible to a job unless ``read_only_paths`` names it.
    """
    return cluster.root / "operator-tools"


#: The interpreter, reached from a venv through an alias directory as uv lays it out.
OPERATOR_VENV_PYTHON = "venv/bin/python"
OPERATOR_INTERPRETER_OUTPUT = "interpreter ran\n"


def write_operator_venv(tools: Path) -> None:
    """Lay out, inside *tools*, a venv whose ``python`` links to an alias of the install."""
    real = tools / "store" / "cpython-3.12.15"
    (real / "bin").mkdir(parents=True, exist_ok=True)
    interpreter = real / "bin" / "python3.12"
    interpreter.write_text(f"#!/bin/sh\necho {OPERATOR_INTERPRETER_OUTPUT}", encoding="utf-8")
    interpreter.chmod(0o755)
    alias = tools / "store" / "cpython-3.12"
    if not alias.is_symlink():
        alias.symlink_to(real.name)
    (tools / "venv" / "bin").mkdir(parents=True, exist_ok=True)
    link = tools / OPERATOR_VENV_PYTHON
    if not link.is_symlink():
        link.symlink_to(alias / "bin" / "python3.12")


def _slurm_config(cluster: SlurmCluster, kind: str, shim: Path | None) -> str:
    """The operator file of *kind*: the SSH transport, or the local one with *shim* as its shell."""
    if shim is None:
        ssh = json.dumps(list(cluster.ssh_command()))
        transport = f'kind = "ssh"\nhost = "{cluster.ssh_host()}"\nssh_command = {ssh}'
    else:
        shell = json.dumps([str(shim), "bash", "-c"])
        transport = f'kind = "local"\nshell_command = {shell}'
    return (
        textwrap.dedent(
            f"""\
            [slurm]
            name = "vibesys-test"
            remote_workspace_root = "{cluster.root}/remote"
            poll_interval_seconds = 0.2

            [slurm.transport]
            """
        )
        + transport
        + '\n\n[vibesys]\nremote_python = "/usr/bin/python3"\n'
        + (
            _AGENT_GPU_TABLE.format(tools=json.dumps(str(operator_tools(cluster))))
            if kind == "slurm-local-gpu"
            else ""
        )
    )


def _slurm_gpu_alias_config(shim: Path) -> str:
    """The deprecated ``[slurm_gpu]`` file, whose wrappers are the login-node shim."""
    return textwrap.dedent(
        f"""\
        [slurm_gpu]
        partitions = ["main"]
        max_gpus = 4
        max_time_minutes = 10
        default_time_minutes = 5
        gate_time_minutes = 5
        gate_gpus = 1
        srun_command = ["{shim}", "srun"]
        scancel_command = ["{shim}", "scancel"]
        """
    )


@contextmanager
def _home(directory: Path) -> Iterator[None]:
    """Point ``HOME`` at *directory* while the alias environment reads its stage root from it.

    The alias stages gates under the user's home, which the cluster must share
    with its nodes; here only the cluster's directory is shared.
    """
    previous = os.environ["HOME"]
    os.environ["HOME"] = str(directory)
    try:
        yield
    finally:
        os.environ["HOME"] = previous


def make_environment(
    kind: str, cluster: SlurmCluster, directory: Path, image_id: str
) -> RunEnvironment:
    """Return the *kind* environment configured for *cluster* (see the module docstring)."""
    docker = DockerEnvironmentConfig(build_runner=PrebuiltImageRunner(image_id))
    shim = None if kind == "slurm" else cluster.write_login_shim(directory)
    if kind == "slurm-gpu-alias":
        config = directory / "slurm-gpu.toml"
        assert shim is not None
        config.write_text(_slurm_gpu_alias_config(shim), encoding="utf-8")
        with _home(cluster.root / "alias-home"):
            return SlurmEnvironment.from_slurm_gpu(
                config,
                RunResourceRequest(accelerators_per_node=1, accelerator_backend="cuda"),
                docker=docker,
            )
    if kind == "slurm-local-gpu":
        tools = operator_tools(cluster)
        tools.mkdir(exist_ok=True)
        (tools / OPERATOR_TOOL_FILE).write_text(OPERATOR_TOOL_TEXT, encoding="utf-8")
        write_operator_venv(tools)
    config = directory / f"{kind}.toml"
    config.write_text(_slurm_config(cluster, kind, shim), encoding="utf-8")
    return SlurmEnvironment(load_slurm_operator_settings(config), docker=docker)


def make_request(
    cluster: SlurmCluster,
    directory: Path,
    run_id: str,
    *,
    accuracy: bool = True,
    log: Callable[[str], None] | None = None,
) -> RunEnvironmentRequest:
    """Build the request: a workspace under the cluster's shared directory."""
    workspace = cluster.root / f"workspace-{run_id}"
    workspace.mkdir()
    log_dir = cluster.root / f"logs-{run_id}"
    log_dir.mkdir()
    return RunEnvironmentRequest(
        log_dir=log_dir,
        workspace=workspace,
        ref_dir=None,
        backend=LocalBackend(
            ComputeBackend.CPU,
            log_dir,
            log=lambda _: None,
            image="python:3.12-slim",
            docker=SubprocessDockerCli(
                processes=OsProcesses(), launcher=SubprocessPipedProcessLauncher()
            ),
            threads=OsThreads(),
            launcher=SubprocessPipedProcessLauncher(),
        ),
        agent_backend="stub",
        cli_provider=None,
        run_id=run_id,
        log=log,
        framework_root=directory / "framework",
        accuracy_command="python3 accuracy.py" if accuracy else None,
        benchmark_command="python3 benchmark.py",
        benchmark_output_argument=BENCHMARK_OUTPUT_ARGUMENT,
        evaluator_requirements=TrustedEvaluatorRequirements(),
        clock=MonotonicClock(),
        system=_real_system(),
    )


def make_candidate_request(
    cluster: SlurmCluster, directory: Path, run_id: str, worktrees: Path
) -> RunEnvironmentRequest:
    """Build the request of one parallel candidate: its worktree under the run's *worktrees*.

    As a product run does, the run owns the whole *worktrees* directory while each
    candidate's workspace is its own subdirectory.
    """
    base = make_request(cluster, directory, run_id)
    workspace = worktrees / f"candidate-{run_id}" / "workspace"
    workspace.mkdir(parents=True)
    return replace(base, workspace=workspace, run_owned_roots=(worktrees,))


@contextmanager
def open_run(
    kind: str,
    cluster: SlurmCluster,
    directory: Path,
    image_id: str,
    *,
    accuracy: bool = True,
) -> Iterator[OpenRun]:
    """Open *kind* as a run would; close the session and remove its containers on exit."""
    run_id = f"t{secrets.token_hex(5)}"
    environment = make_environment(kind, cluster, directory, image_id)
    lines: list[str] = []
    request = make_request(cluster, directory, run_id, accuracy=accuracy, log=lines.append)
    with _opened(kind, cluster, environment, request) as run:
        yield replace(run, log_lines=tuple(lines))


@contextmanager
def _opened(
    kind: str, cluster: SlurmCluster, environment: RunEnvironment, request: RunEnvironmentRequest
) -> Iterator[OpenRun]:
    session = open_run_environment(environment, request)
    try:
        yield OpenRun(session, request, request.run_id, kind)
    finally:
        try:
            session.close()
        finally:
            remove_run_containers(request.run_id)
            cluster.cancel_all()


@contextmanager
def open_candidates(
    kind: str, cluster: SlurmCluster, directory: Path, image_id: str, *, count: int
) -> Iterator[list[OpenRun]]:
    """Open *count* parallel candidate sessions of *kind*, alive together.

    Each candidate has its own worktree under one worktrees directory, which the
    run owns, and its own container and broker, as the dynamic loop opens them.
    """
    tag = secrets.token_hex(5)
    environment = make_environment(kind, cluster, directory, image_id)
    worktrees = cluster.root / f"worktrees-{tag}"
    sessions: list[OpenRun] = []
    try:
        for index in range(count):
            run_id = f"t{tag}c{index}"
            request = make_candidate_request(cluster, directory, run_id, worktrees)
            sessions.append(
                OpenRun(open_run_environment(environment, request), request, run_id, kind)
            )
        yield sessions
    finally:
        try:
            for opened in reversed(sessions):
                opened.session.close()
        finally:
            for opened in sessions:
                remove_run_containers(opened.run_id)
            cluster.cancel_all()


def _real_system() -> EnvironmentSystem:
    """The host's own threads, sockets and processes.

    The agent GPU launcher checks and cancels its jobs with ``squeue`` and ``scancel``
    through ``slurm_processes``, so they reach the real cluster too.
    """
    processes = OsProcesses()
    return EnvironmentSystem(
        threads=OsThreads(),
        network=UnixNetwork(),
        processes=processes,
        launcher=SubprocessPipedProcessLauncher(),
        slurm_processes=processes,
    )
