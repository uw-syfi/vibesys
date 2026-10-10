"""Open the editor container of a run on a host without GPUs, from a real agent image.

The editor is opened through ``open_run_environment`` exactly as a run opens it:
the real ``docker build`` of the agent layer on the backend's default base image,
the real mounts, environment and user remapping, and a real host broker whose
``srun`` is a local program. What a run on a GPU host has and this does not is the
accelerator: the editor of the ``slurm`` environment with agent GPU commands holds none (its GPU work
is a job), so the CUDA and ROCm bases are built and started without a device.
"""

from __future__ import annotations

import secrets
import shlex
import sys
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

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
    RunEnvironmentRequest,
    RunEnvironmentSession,
    SlurmEnvironment,
    TrustedEvaluatorRequirements,
)
from vs_sandbox.api import ComputeBackend, ComputeBackendImpl, DockerSandbox, create_compute_backend
from vs_sandbox.api.slurm import load_slurm_operator_settings

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from vs_mcp.api import ToolServerDescriptor

#: The label ``DockerSandbox`` puts on its containers (``vs_sandbox.docker_sandbox.RUN_ID_LABEL``).
RUN_ID_LABEL = "vibesys.run-id"
#: Every run id the tier uses starts with this, so a script can find what an interrupted run left.
RUN_ID_PREFIX = "minimal-container-"
#: Longest a base image pull may take; the ROCm default is tens of gigabytes.
PULL_TIMEOUT_SECONDS = 3600.0

#: Stands in for ``srun`` and ``scancel``: runs what follows ``--`` right here.
_FAKE_SLURM = textwrap.dedent(
    """\
    import os, sys
    if "--" in sys.argv:
        command = sys.argv[sys.argv.index("--") + 1 :]
        os.execvp(command[0], command)
    """
)
#: Stands in for ``vs_sandbox.slurm_command``: runs the planned gate right here.
_FAKE_GATE = textwrap.dedent(
    """\
    import json, os, sys
    plan = json.load(open(sys.argv[sys.argv.index("--plan") + 1]))
    kind = sys.argv[sys.argv.index("--plan") + 2]
    command = plan[kind + "_command"] + sys.argv[sys.argv.index("--plan") + 3 :]
    os.execvp(command[0], command)
    """
)
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


#: The default base image of every backend that has a container, then stand-ins.
BASES = (
    Base("cpu", ComputeBackend.CPU),
    Base("cuda", ComputeBackend.CUDA),
    Base("rocm", ComputeBackend.ROCM),
    Base("ubuntu-uid-1000", ComputeBackend.CPU, image="ubuntu:24.04", stand_in=True),
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
        result = self.sandbox.execute(command, timeout=300)
        assert result.exit_code is not None, result.output
        return result.exit_code, result.output

    @property
    def gpu_client(self) -> str:
        """The ``vibesys-gpu`` broker client the editor was given: the program of its gate commands."""
        paths = self.session.view.paths
        command = paths.accuracy_command or paths.benchmark_command
        assert command is not None
        return shlex.split(command)[0]

    def gate(self, kind: str, *arguments: str) -> tuple[int, str]:
        """Run the agent's *kind* gate (``accuracy`` or ``benchmark``) with *arguments*."""
        return self.run(shlex.join((self.gpu_client, "--gate", kind, *arguments)))


def ensure_base_image(image: str) -> None:
    """Pull *image* only when the local store lacks it (a pull is slow and large)."""
    if docker("image", "inspect", image, check=False).returncode != 0:
        docker("pull", image, timeout=PULL_TIMEOUT_SECONDS)


def make_backend(base: Base, log_dir: Path) -> ComputeBackendImpl:
    """The compute backend of *base*, configured as a run on a host without its GPU is."""
    return create_compute_backend(base.backend, log_dir, log=lambda _: None, image=base.image)


def _slurm_config(directory: Path) -> Path:
    fake = directory / "fake_slurm.py"
    fake.write_text(_FAKE_SLURM, encoding="utf-8")
    config = directory / "slurm.toml"
    config.write_text(
        textwrap.dedent(
            f"""\
            [slurm]
            name = "minimal-container"
            remote_workspace_root = "{directory / "stage"}"

            [slurm.transport]
            kind = "local"

            [vibesys.agent_gpu]
            partitions = ["main"]
            max_gpus = 8
            max_time_minutes = 120
            srun_command = ["{sys.executable}", "{fake}"]
            scancel_command = ["{sys.executable}", "{fake}"]
            """
        ),
        encoding="utf-8",
    )
    return config


@contextmanager
def open_editor(base: Base, directory: Path) -> Iterator[Editor]:
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
    )
    gate = directory / "fake_gate.py"
    gate.write_text(_FAKE_GATE, encoding="utf-8")
    environment = SlurmEnvironment(
        load_slurm_operator_settings(_slurm_config(directory)),
        gate_wrapper=(sys.executable, str(gate)),
        docker=DockerEnvironmentConfig(),
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
