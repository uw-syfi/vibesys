"""Open the editor container of a run on a host without GPUs, from a real agent image.

The editor is opened through ``open_run_environment`` exactly as a run opens it:
the real ``docker build`` of the agent layer on the backend's default base image,
the real mounts, environment and user remapping, and a real host broker whose
``srun`` is a local program. What a run on a GPU host has and this does not is the
accelerator: the editor of the ``slurm`` environment with agent GPU commands holds none (its GPU work
is a job), so the CUDA and ROCm bases are built and started without a device.
"""

from __future__ import annotations

import json
import secrets
import sys
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from scripts.ci_base_images import base_image
from tests.slurm_cluster.cluster import docker

from vibesys.config import BUNDLED_RESOURCES, PROJECT_ROOT
from vibesys.orchestration.profilers import (
    default_profiler_for_backend,
    profiler_definition,
    profiler_support_extra,
)
from vibesys.run.environment import open_run_environment
from vs_agent.api import MCPServerSpec, containerize_server
from vs_mcp.api import StdioServerDescriptor
from vs_runtime.api.infrastructure import (
    DockerEnvironmentConfig,
    EnvironmentSystem,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    SlurmEnvironment,
    TrustedEvaluatorRequirements,
)
from vs_sandbox.api import (
    ComputeBackend,
    ComputeBackendImpl,
    DockerSandbox,
    SubprocessDockerCli,
    create_compute_backend,
)
from vs_sandbox.api.slurm import load_slurm_operator_settings
from vs_sim.api import (
    MonotonicClock,
    OsProcesses,
    OsThreads,
    SubprocessPipedProcessLauncher,
    SystemClock,
    UnixNetwork,
)
from vs_sim.api.testing import ScriptedProbe
from vs_slurm.api import FakeConnector

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from vs_mcp.api import ToolServerDescriptor
from vs_sim.api import DISCARD

#: The label ``DockerSandbox`` puts on its containers (``vs_sandbox.docker_sandbox.RUN_ID_LABEL``).
RUN_ID_LABEL = "vibesys.run-id"
#: Every run id the tier uses starts with this, so a script can find what an interrupted run left.
RUN_ID_PREFIX = "minimal-container-"
#: Longest a base image pull may take; the ROCm default is tens of gigabytes.
PULL_TIMEOUT_SECONDS = 3600.0
#: Budget for the agent image build. The first test of the tier pays for the
#: build inside pytest's per-test limit (300 s, `timeout` in pyproject.toml), and a
#: cold build takes about 140 s, so a stalled build must end by this budget
#: with its output in the error, not by pytest's limit with nothing.
IMAGE_BUILD_BUDGET_SECONDS = 240.0

ACCURACY_SCRIPT = 'print("accuracy ok")\n'
BENCHMARK_SCRIPT = textwrap.dedent(
    """\
    import json, sys
    output = sys.argv[sys.argv.index("--output") + 1]
    with open(output, "w") as handle:
        json.dump({"score": 42}, handle)
    print("benchmark wrote", output)
    """
)


class PassThroughConfinement:
    """Job confinement that runs the command as given (the fake ``srun`` is the cluster)."""

    def wrap(self, workspace: Path, argv: Sequence[str]) -> list[str]:
        """Return *argv* unchanged."""
        del workspace
        return list(argv)


@dataclass(frozen=True)
class Base:
    """One base image the agent layer is built on, and the backend that names it.

    *image* is ``None`` for a backend's default base image. A stand-in names a small
    image with the same property as a default one (here, an Ubuntu 24.04 image that
    already owns uid 1000), so that property is covered without a multi-gigabyte pull.
    """

    name: str
    backend: ComputeBackend
    image: str | None = None
    stand_in: bool = False


#: Every container backend's base, with pinned CI mirrors for CPU and stand-ins.
BASES = (
    Base("cpu", ComputeBackend.CPU, image=base_image("python:3.12-bookworm")),
    Base("cuda", ComputeBackend.CUDA),
    Base("rocm", ComputeBackend.ROCM),
    Base("ubuntu-uid-1000", ComputeBackend.CPU, image=base_image("ubuntu:24.04"), stand_in=True),
)


@dataclass(frozen=True)
class Editor:
    """One opened editor container and what a test needs to start processes in it."""

    base: Base
    base_image: str
    session: RunEnvironmentSession
    request: RunEnvironmentRequest
    profiler_support_name: str

    @property
    def sandbox(self) -> DockerSandbox:
        """The editor container."""
        return cast("DockerSandbox", self.session.sandbox)

    def argv(
        self,
        command: Sequence[str],
        *,
        env: Sequence[tuple[str, str]] = (),
        cwd: Path | None = None,
    ) -> list[str]:
        """Wrap *command* as a ``docker exec -i`` call, with *env* set for it alone."""
        inner = ["env", *(f"{key}={value}" for key, value in env), *command] if env else command
        return self.sandbox.wrap(list(inner), cwd or self.request.workspace)

    def server_argv(self, descriptor: ToolServerDescriptor) -> list[str]:
        """Start *descriptor*'s server the way the agent launcher starts it in this container."""
        assert isinstance(descriptor, StdioServerDescriptor)
        started = containerize_server(
            MCPServerSpec(
                descriptor.name,
                descriptor.command,
                descriptor.args,
                descriptor.env,
                descriptor.runtime_env,
            )
        )
        return self.argv([started.command, *started.args], env=(*started.env, *started.runtime_env))

    def run(self, command: str) -> tuple[int, str]:
        """Run a shell *command* as the agent; return its exit status and output."""
        result = self.sandbox.execute(command, timeout=300, output=DISCARD)
        assert result.exit_code is not None, result.output
        return result.exit_code, result.output


def ensure_base_image(image: str) -> None:
    """Pull *image* only when the local store lacks it (a pull is slow and large)."""
    if docker("image", "inspect", image, check=False).returncode != 0:
        docker("pull", image, timeout=PULL_TIMEOUT_SECONDS)


def make_backend(base: Base, log_dir: Path) -> ComputeBackendImpl:
    """The compute backend of *base*, configured as a run on a host without its GPU is."""
    return create_compute_backend(
        base.backend,
        log_dir,
        log=lambda _: None,
        image=base.image,
        clock=SystemClock(),
        docker=SubprocessDockerCli(
            processes=OsProcesses(), launcher=SubprocessPipedProcessLauncher()
        ),
        threads=OsThreads(),
        launcher=SubprocessPipedProcessLauncher(),
        probe=ScriptedProbe(lambda _argv: None),
    )


def _slurm_config(directory: Path) -> Path:
    cluster = FakeConnector(directory / "cluster").state
    shell = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(cluster), "shell"])
    rsync = json.dumps([sys.executable, "-m", "vs_slurm.fake_connector", str(cluster), "rsync"])
    config = directory / "slurm.toml"
    config.write_text(
        textwrap.dedent(
            f"""\
            [slurm]
            name = "minimal-container"
            remote_workspace_root = "{directory / "stage"}"

            [slurm.transport]
            kind = "local"
            shell_command = {shell}
            rsync_command = {rsync}

            [vibesys.agent_gpu]
            partitions = ["main"]
            max_gpus = 8
            max_time_minutes = 120
            """
        ),
        encoding="utf-8",
    )
    return config


@contextmanager
def open_editor(
    base: Base,
    directory: Path,
    *,
    image_cache_from: tuple[str, ...] = (),
) -> Iterator[Editor]:
    """Build the agent image on *base*'s default image, open the editor, and close it."""
    run_id = f"{RUN_ID_PREFIX}{secrets.token_hex(5)}"
    workspace = directory / "workspace"
    workspace.mkdir()
    (workspace / "accuracy.py").write_text(ACCURACY_SCRIPT, encoding="utf-8")
    (workspace / "benchmark.py").write_text(BENCHMARK_SCRIPT, encoding="utf-8")
    log_dir = directory / "logs"
    log_dir.mkdir()
    backend = make_backend(base, log_dir)
    base_image = getattr(backend, "image", None)
    assert isinstance(base_image, str), "the backend has no base image"
    ensure_base_image(base_image)
    definition = profiler_definition(default_profiler_for_backend(base.backend))
    support = BUNDLED_RESOURCES.directory("profilers", definition.kind.value)
    assert support is not None, f"no bundled support directory for {definition.kind.value}"
    request = RunEnvironmentRequest(
        log_dir=log_dir,
        workspace=workspace,
        ref_dir=None,
        backend=backend,
        agent_backend="cli",
        cli_provider="claude",
        run_id=run_id,
        framework_root=PROJECT_ROOT,
        accuracy_command="python3 accuracy.py",
        benchmark_command="python3 benchmark.py",
        benchmark_output_argument="--output",
        evaluator_requirements=TrustedEvaluatorRequirements(),
        profiler_support_path=str(support),
        profiler_support_name=definition.support_name,
        profiler_support_extra=profiler_support_extra(definition),
        clock=MonotonicClock(),
        system=_real_system(),
    )
    environment = SlurmEnvironment(
        load_slurm_operator_settings(_slurm_config(directory)),
        docker=DockerEnvironmentConfig(
            image_build_timeout_seconds=IMAGE_BUILD_BUDGET_SECONDS,
            image_cache_from=image_cache_from,
        ),
        job_confinement=PassThroughConfinement(),
    )
    session = open_run_environment(environment, request)
    try:
        yield Editor(
            base=base,
            base_image=base_image,
            session=session,
            request=request,
            profiler_support_name=definition.support_name,
        )
    finally:
        try:
            session.close()
        finally:
            ids = docker("ps", "-aq", "--filter", f"label={RUN_ID_LABEL}={run_id}").stdout.split()
            if ids:
                docker("rm", "-f", "-v", *ids, check=False)


def _real_system() -> EnvironmentSystem:
    """The host's own threads, sockets and processes, including the executable Fake cluster."""
    return EnvironmentSystem(
        threads=OsThreads(),
        network=UnixNetwork(),
        processes=OsProcesses(),
        launcher=SubprocessPipedProcessLauncher(),
        slurm_processes=OsProcesses(),
    )
