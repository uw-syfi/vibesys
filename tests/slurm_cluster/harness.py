"""Open the ``slurm`` and ``slurm-gpu`` run environments against a real cluster.

Both environments are opened through ``open_run_environment`` exactly as a run
opens them. What differs from production is only what the tier cannot have on a
single host: the agent image carries no agent CLI, and the host reaches Slurm
through the operator-configured command prefixes (``ssh`` for ``slurm``, the
login-node shim for ``slurm-gpu``). The agent side is a real Docker container;
tests drive it with scripted commands through the session's sandbox.
"""

from __future__ import annotations

import json
import secrets
import shlex
import subprocess
import textwrap
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from tests.slurm_cluster.cluster import SlurmCluster, build_image, docker

from vibesys.run.environment import open_run_environment
from vs_project.api import RunResourceRequest
from vs_runtime.api.infrastructure import (
    DockerEnvironmentConfig,
    RunEnvironment,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    SlurmEnvironment,
    SlurmGpuEnvironment,
    TrustedEvaluatorRequirements,
)
from vs_sandbox.api import ComputeBackend, DockerSandbox, LocalBackend

if TYPE_CHECKING:
    import threading
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from vs_sandbox.api import CommandResult
#: The label ``DockerSandbox`` puts on its containers (``vs_sandbox.docker_sandbox.RUN_ID_LABEL``).
_RUN_ID_LABEL = "vibesys.run-id"
BENCHMARK_OUTPUT_ARGUMENT = "--output"
#: The container's own ``/tmp``, where the framework's benchmark result is relayed to.
CONTAINER_TMP = "/tmp"  # noqa: S108  # lint-waiver: LW-960011 [S108]; the framework result location is fixed, and it is this directory in the agent container.
# > A tempfile call would name the test process's directory, not the container's.
# Both gate scripts take their behavior from a file in their working directory, so
# a test steers the gate (and, for ``slurm``, what is staged to the cluster) by
# writing the file before it runs the gate. ``hold`` blocks until cancelled,
# ``env`` prints the job's environment, ``exit:N`` fails with status N.
ACCURACY_SCRIPT = textwrap.dedent(
    """\
    import os, pathlib, sys, time
    path = pathlib.Path("accuracy_mode")
    mode = path.read_text().strip() if path.exists() else "pass"
    if mode == "hold":
        print("holding", flush=True)
        time.sleep(600)
    elif mode == "env":
        print("\\n".join(f"{key}={value}" for key, value in sorted(os.environ.items())))
    elif mode.startswith("exit:"):
        print("accuracy failed")
        sys.exit(int(mode.removeprefix("exit:")))
    else:
        print("accuracy ok")
    """
)
# Writes the result file named by --output, and prints where it ran.
BENCHMARK_SCRIPT = textwrap.dedent(
    """\
    import json, os, pathlib, sys
    output = sys.argv[sys.argv.index("--output") + 1]
    with open(output, "w") as handle:
        json.dump({"score": 42, "cwd": os.getcwd()}, handle)
    print("benchmark wrote", output)
    path = pathlib.Path("benchmark_mode")
    mode = path.read_text().strip() if path.exists() else "pass"
    sys.exit(int(mode.removeprefix("exit:")) if mode.startswith("exit:") else 0)
    """
)


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
        self, argv: Sequence[str], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        """Return the scripted outcome for ``docker build`` or ``docker image inspect``."""
        del cwd, timeout
        stdout = self._image_id if argv[1] == "image" else ""
        return subprocess.CompletedProcess(tuple(argv), 0, stdout, "")


@dataclass(frozen=True)
class OpenRun:
    """One opened run: its session, its request, and the inputs that made them."""

    session: RunEnvironmentSession
    request: RunEnvironmentRequest
    run_id: str

    def agent(
        self, command: str, *, cancel: threading.Event | None = None, timeout: int | None = None
    ) -> CommandResult:
        """Run a scripted *command* as the agent, in the container, and return its result."""
        return self.session.sandbox.execute(command, timeout=timeout, cancel=cancel)

    def launcher(self) -> str:
        """The broker client the agent runs for gates: the program named by its gate commands."""
        paths = self.session.view.paths
        command = paths.benchmark_command or paths.accuracy_command
        assert command is not None
        return shlex.split(command)[0]

    def gate(self, kind: str, *arguments: str) -> CommandResult:
        """Run the agent's gate command for *kind* (planned or not) with *arguments*."""
        return self.agent(shlex.join((self.launcher(), "--gate", kind, *arguments)))

    def set_mode(self, gate: str, mode: str) -> None:
        """Steer the next *gate* (``accuracy`` or ``benchmark``) run: ``pass``, ``hold``, ``env`` or ``exit:N``."""
        (self.workspace / f"{gate}_mode").write_text(mode, encoding="utf-8")

    def reset_modes(self) -> None:
        """Forget every steered gate mode, so the next gate takes the default (``pass``).

        A run is shared by many tests; a mode one test set (``hold`` above all)
        would otherwise make a later test's gate block until the hang guard.
        """
        for gate in ("accuracy", "benchmark"):
            (self.workspace / f"{gate}_mode").unlink(missing_ok=True)

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


def _slurm_config(cluster: SlurmCluster) -> str:
    ssh = json.dumps(list(cluster.ssh_command()))
    return textwrap.dedent(
        f"""\
        [slurm]
        name = "vibesys-test"
        remote_workspace_root = "{cluster.root}/remote"
        poll_interval_seconds = 0.2

        [slurm.transport]
        kind = "ssh"
        host = "{cluster.ssh_host()}"
        ssh_command = {ssh}

        [vibesys]
        remote_python = "/usr/bin/python3"
        """
    )


def _slurm_gpu_config(shim: Path) -> str:
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


def make_environment(
    kind: str, cluster: SlurmCluster, directory: Path, image_id: str
) -> RunEnvironment:
    """Return the *kind* (``slurm`` or ``slurm-gpu``) environment configured for *cluster*."""
    docker = DockerEnvironmentConfig(build_runner=PrebuiltImageRunner(image_id))
    if kind == "slurm":
        config = directory / "slurm.toml"
        config.write_text(_slurm_config(cluster), encoding="utf-8")
        return SlurmEnvironment(config, docker=docker)
    config = directory / "slurm-gpu.toml"
    config.write_text(_slurm_gpu_config(cluster.write_login_shim(directory)), encoding="utf-8")
    return SlurmGpuEnvironment(
        config,
        RunResourceRequest(accelerators_per_node=1, accelerator_backend="cuda"),
        docker=docker,
    )


def make_request(
    cluster: SlurmCluster,
    directory: Path,
    run_id: str,
    *,
    accuracy: bool = True,
) -> RunEnvironmentRequest:
    """Build the request: a workspace under the cluster's shared directory, planned gates."""
    workspace = cluster.root / f"workspace-{run_id}"
    workspace.mkdir()
    (workspace / "accuracy.py").write_text(ACCURACY_SCRIPT, encoding="utf-8")
    (workspace / "benchmark.py").write_text(BENCHMARK_SCRIPT, encoding="utf-8")
    log_dir = cluster.root / f"logs-{run_id}"
    log_dir.mkdir()
    return RunEnvironmentRequest(
        log_dir=log_dir,
        workspace=workspace,
        ref_dir=None,
        backend=LocalBackend(
            ComputeBackend.CPU, log_dir, log=lambda _: None, image="python:3.12-slim"
        ),
        agent_backend="stub",
        cli_provider=None,
        run_id=run_id,
        framework_root=directory / "framework",
        accuracy_command="python3 accuracy.py" if accuracy else None,
        benchmark_command="python3 benchmark.py",
        benchmark_output_argument=BENCHMARK_OUTPUT_ARGUMENT,
        evaluator_requirements=TrustedEvaluatorRequirements(),
    )


@contextmanager
def open_run(
    kind: str,
    cluster: SlurmCluster,
    directory: Path,
    image_id: str,
    *,
    accuracy: bool = True,
) -> Iterator[OpenRun]:
    """Open *kind* against *cluster*; close the session and remove its containers on exit."""
    run_id = f"t{secrets.token_hex(5)}"
    environment = make_environment(kind, cluster, directory, image_id)
    request = make_request(cluster, directory, run_id, accuracy=accuracy)
    session = open_run_environment(environment, request)
    try:
        yield OpenRun(session, request, run_id)
    finally:
        try:
            session.close()
        finally:
            remove_run_containers(run_id)
            cluster.cancel_all()
