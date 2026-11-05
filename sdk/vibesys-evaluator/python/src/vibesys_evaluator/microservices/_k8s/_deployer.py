"""The k8s implementation of the orchestrator's deploy interface (`_private.deploy`).

`KubeDeployer` is the `Deployer`: it brings an App up for one sample (`deploy()`), and
hands out a `KubePhaseRunner`, the `PhaseRunner` that runs each phase in a pod of the
namespace. `control` runs `bench.py ctl ...` against a sample's namespace.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibesys_evaluator._private.files import DelimitedWriter, SchemaFileError, phase_input
from vibesys_evaluator._private.http import step_message
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator.microservices._k8s._build import BuildctlBuilder
from vibesys_evaluator.microservices._k8s._clock import SystemClock
from vibesys_evaluator.microservices._k8s._deployment import (
    AppSource,
    Cluster,
    Deployment,
    Environment,
    PodCommand,
    PodOutOfMemoryError,
    attach,
    ctl,
    deploy,
)
from vibesys_evaluator.microservices._k8s._kubectl import KubectlAPI
from vibesys_evaluator.microservices._k8s._pods import WORK_DIR, Role
from vibesys_evaluator.microservices._private.deploy import (
    PHASE_INPUT_FILE,
    PHASE_LOG_FILE,
    DeployContext,
    DeployError,
    PhaseJob,
    PhaseRunError,
    TaskCommand,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence

    from vibesys_evaluator.microservices import Step
    from vibesys_evaluator.microservices.k8s import App

NAMESPACE_VARIABLE = "VIBESYS_EVALUATOR_NAMESPACE"  # the sample `ctl` acts on
NAMESPACE_FILE = "namespace.txt"


def connect(env: Environment, log: Callable[[str], None]) -> Cluster:
    """The real cluster: kubectl with the job's kubeconfig, buildctl at the job's builder."""
    clock = SystemClock()
    return Cluster(
        env=env,
        kube=KubectlAPI(),
        builder=BuildctlBuilder(env.builder, clock),
        clock=clock,
        log=log,
    )


def probe_plan(steps: Sequence[Step]) -> bytes:
    """The ready steps as a recorded request stream for ``loadgen probe``: one operation per step."""
    buffer = io.BytesIO()
    writer = DelimitedWriter(buffer)
    writer.write_header(pb.FileKind.REQUESTS)
    writer.write(pb.RequestsHeader(operation_names=[f"ready_{n}" for n in range(len(steps))]))
    for n, step in enumerate(steps):
        writer.write(
            pb.PlannedOperation(operation=n, steps=[step_message(step)], keep_bodies=False)
        )
    return buffer.getvalue()


def _out_of_memory(job: PhaseJob, error: PodOutOfMemoryError) -> str:
    """What an OOMKilled phase pod means: the limit, the phase, and what to change."""
    try:
        phase = phase_input(job.phase_input, job.name)
        span = f"a phase of {phase.warmup_secs:g} s warmup and {phase.secs:g} s window"
    except SchemaFileError:
        span = "a phase"
    return (
        f"{job.name}: the phase pod ran out of memory (OOMKilled at its {error.limit} limit) "
        f"in {span}. loadgen holds only its buffer of the request stream; what must fit is the "
        "generator's dataset and model (with the history it replays) and the results of the "
        "sampled operations and writes the check keeps: shrink the model, sample fewer large "
        f"responses (Operation.sample), or shorten the window. {error}"
    )


@dataclass(frozen=True)
class KubePhaseRunner:
    """Runs each phase as `task.phase(...)` in a phase pod (6 CPUs, 4 GiB) of the namespace."""

    deployment: Deployment
    task: TaskCommand

    def run_phase(self, job: PhaseJob, out_dir: Path) -> None:
        """Run the phase; copy the files it left in its work directory and its log into out_dir."""
        command = PodCommand(
            argv=self.task.phase(work_dir=WORK_DIR, target=self.deployment.gateway.url),
            inputs={PHASE_INPUT_FILE: job.phase_input},
        )
        try:
            run = self.deployment.run_pod(
                Role.PHASE, command, timeout_secs=job.timeout_secs, outputs=out_dir
            )
        except PodOutOfMemoryError as error:
            (out_dir / PHASE_LOG_FILE).write_text(str(error))
            raise PhaseRunError(_out_of_memory(job, error)) from None
        except DeployError as error:
            (out_dir / PHASE_LOG_FILE).write_text(str(error))
            raise PhaseRunError(f"{job.name}: {error}") from None
        (out_dir / PHASE_LOG_FILE).write_text(run.log)
        self.deployment.usage(job.name)
        if run.exit_code != 0:
            raise PhaseRunError(
                f"{job.name} exited {run.exit_code} after {run.seconds:.0f}s; "
                f"log tail:\n{run.log[-1500:]}"
            )


class KubeDeployer:
    """Brings an App up in a fresh namespace per sample (the `Deployer` of `microservices.k8s`)."""

    def __init__(
        self,
        app: App[Any],
        *,
        environ: Mapping[str, str] | None = None,
        connect: Callable[[Environment, Callable[[str], None]], Cluster] = connect,
    ) -> None:
        self._app = app
        self._environ = environ if environ is not None else os.environ
        self._connect = connect

    @contextmanager
    def deploy(self, context: DeployContext) -> Iterator[KubePhaseRunner]:
        """Sweep, build, deploy, seed and probe; yield the phase runner; delete the namespace."""
        with tempfile.TemporaryDirectory(prefix="vse-deploy-") as scratch:
            env = Environment.parse(
                self._environ, work_dir=Path(scratch), artifacts=context.artifacts
            )
            cluster = self._connect(env, context.log)
            source = AppSource(
                app=self._app,
                task_dir=Path(context.task.script).parent,
                project_root=context.project_root,
                probe_plan=probe_plan(self._app.ready),
            )
            seed = (
                PodCommand(argv=context.task.seed(dataset_digest=context.dataset_digest))
                if self._app.seed is not None
                else None
            )
            with deploy(source, seed=seed, cluster=cluster) as deployment:
                deployment.write_artifact(NAMESPACE_FILE, deployment.namespace + "\n")
                context.log(f"serving in namespace {deployment.namespace}")
                yield KubePhaseRunner(deployment=deployment, task=context.task)


def deployer(app: App[Any]) -> KubeDeployer:
    """The deployer that brings `app` up in a fresh namespace per sample."""
    return KubeDeployer(app)


def run_control(
    app: App[Any],
    argv: Sequence[str],
    *,
    environ: Mapping[str, str],
    task_dir: Path,
    connect: Callable[[Environment, Callable[[str], None]], Cluster] = connect,
) -> int:
    """`bench.py ctl ARGV` against the namespace in $VIBESYS_EVALUATOR_NAMESPACE; exit status."""

    def log(line: str) -> None:
        print(f"vibesys-evaluator ctl: {line}", file=sys.stderr, flush=True)

    namespace = environ.get(NAMESPACE_VARIABLE, "")
    if not namespace:
        log(
            f"{NAMESPACE_VARIABLE} is not set: name the sample's namespace ({NAMESPACE_FILE} "
            "in its artifacts)"
        )
        return 2
    with tempfile.TemporaryDirectory(prefix="vse-ctl-") as scratch:
        try:
            env = Environment.parse(environ, work_dir=Path(scratch), artifacts=None)
            source = AppSource(
                app=app, task_dir=task_dir, project_root=task_dir, probe_plan=probe_plan(app.ready)
            )
            ctl(attach(source, namespace=namespace, cluster=connect(env, log)), argv)
        except DeployError as error:
            log(str(error))
            return 1
    return 0


def control(app: App[Any], argv: Sequence[str]) -> int:
    """Run `bench.py ctl ARGV` (restart, flush, stop, start) against a running sample."""
    return run_control(app, argv, environ=os.environ, task_dir=Path(sys.argv[0]).resolve().parent)
