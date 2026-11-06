"""The k8s implementation of the orchestrator's deploy interface (`_private.deploy`), over the Go core.

`KubeDeployer` is the `Deployer`: `deploy()` runs `vibesys-evaluator k8s deploy` (sweep, build,
deploy, seed, probe) and hands out a `KubePhaseRunner`, the `PhaseRunner` that runs each phase in a
pod of the namespace with `k8s run-pod`; leaving the context runs `k8s teardown`. `control` runs
`bench.py ctl ...` against a sample's namespace.
"""

from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibesys_evaluator._private.files import SchemaFileError, phase_input
from vibesys_evaluator.microservices._k8s._deployment import (
    NAMESPACE_VARIABLE,
    WORK_DIR,
    Deployment,
    OwnedDeployment,
    PodOutOfMemoryError,
    app_message,
    attach,
    call,
    probe_plan,
)
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
    from collections.abc import Callable, Iterator, Sequence

    from vibesys_evaluator.microservices.k8s import App

NAMESPACE_FILE = "namespace.txt"
CTL_USAGE = "ctl restart DEPLOYMENT [--every-pod] | flush CACHE | stop | start"
#: Slack over a phase's own timeout for its pod to start and its files to be copied back.
POD_OVERHEAD_SECS = 1800.0


def _out_of_memory(job: PhaseJob, error: PodOutOfMemoryError) -> str:
    """What an OOMKilled phase pod means: the limit, the phase, and what to change."""
    try:
        phase = phase_input(job.phase_input, job.name)
        span = f"a phase of {phase.warmup_secs:g} s warmup and {phase.secs:g} s window"
    except SchemaFileError:
        span = "a phase"
    return (
        f"{job.name}: the phase pod ran out of memory (OOMKilled at its {error.limit} limit) "
        f"in {span}. What must fit is loadgen's buffer of the request stream and the "
        "generator's dataset and model: shrink the model or the dataset, lower the load "
        f"(clients or rate), or shorten the window. {error}"
    )


@dataclass(frozen=True)
class KubePhaseRunner:
    """Runs each phase as `task.phase(...)` in a phase pod (6 CPUs, 4 GiB) of the namespace."""

    deployment: OwnedDeployment
    task: TaskCommand
    scratch: Path

    def run_phase(self, job: PhaseJob, out_dir: Path) -> None:
        """Run the phase; copy the files it left in its work directory and its log into out_dir."""
        input_file = self.scratch / f"{job.name}.{PHASE_INPUT_FILE}"
        input_file.write_bytes(job.phase_input)
        argv = self.task.phase(work_dir=WORK_DIR, target=self.deployment.gateway_url)
        try:
            run = self.deployment.run_pod(
                "phase",
                argv,
                inputs={PHASE_INPUT_FILE: input_file},
                timeout_secs=job.timeout_secs,
                outputs=out_dir,
            )
        except PodOutOfMemoryError as error:
            (out_dir / PHASE_LOG_FILE).write_text(str(error))
            raise PhaseRunError(_out_of_memory(job, error)) from None
        except DeployError as error:
            (out_dir / PHASE_LOG_FILE).write_text(str(error))
            raise PhaseRunError(f"{job.name}: {error}") from None
        log = str(run.get("log", ""))
        exit_code = int(run.get("exit_code", 0))
        (out_dir / PHASE_LOG_FILE).write_text(log)
        self.deployment.usage(job.name)
        if exit_code != 0:
            raise PhaseRunError(
                f"{job.name} exited {exit_code} after {float(run.get('seconds', 0)):.0f}s; "
                f"log tail:\n{log[-1500:]}"
            )


class KubeDeployer:
    """Brings an App up in a fresh namespace per sample (the `Deployer` of `microservices.k8s`).

    The cluster comes from the environment the core reads: KUBECONFIG, VIBESYS_K8S_REGISTRY and
    VIBESYS_K8S_BUILDER.
    """

    def __init__(self, app: App[Any]) -> None:
        """Deploy ``app``."""
        self._app = app

    @contextmanager
    def deploy(self, context: DeployContext) -> Iterator[KubePhaseRunner]:
        """Sweep, build, deploy, seed and probe; yield the phase runner; delete the namespace."""
        with tempfile.TemporaryDirectory(prefix="vse-deploy-") as scratch_name:
            scratch = Path(scratch_name)
            plan = scratch / "probe.binpb"
            plan.write_bytes(probe_plan(self._app.ready))
            request: dict[str, Any] = {
                "app": app_message(self._app),
                "task_dir": str(Path(context.task.script).parent),
                "project_root": str(context.project_root),
                "work_dir": str(scratch / "core"),
                "artifacts_dir": str(context.artifacts),
                "probe_plan_file": str(plan),
            }
            if self._app.seed is not None:
                request["seed"] = {
                    "argv": list(context.task.seed(dataset_digest=context.dataset_digest))
                }
            reply = call("deploy", request, grace=True)
            deployment = OwnedDeployment(self._app, reply["target"])
            failure: BaseException | None = None
            try:
                (context.artifacts / NAMESPACE_FILE).write_text(deployment.namespace + "\n")
                context.log(f"serving in namespace {deployment.namespace}")
                yield KubePhaseRunner(deployment=deployment, task=context.task, scratch=scratch)
            except BaseException as error:
                failure = error
                raise
            finally:
                _close(deployment, failure, context.log)


def _close(
    deployment: OwnedDeployment, failure: BaseException | None, log: Callable[[str], None]
) -> None:
    """Delete the namespace; after a signal only ask for it, and never hide the original failure."""
    stopping = failure is not None and not isinstance(failure, Exception)
    try:
        deployment.teardown(fast=stopping, failed=failure is not None)
    except DeployError as error:
        if failure is None:
            raise
        log(f"teardown failed: {error}")


def deployer(app: App[Any]) -> KubeDeployer:
    """The deployer that brings `app` up in a fresh namespace per sample."""
    return KubeDeployer(app)


def run_control(
    app: App[Any],
    argv: Sequence[str],
    *,
    environ: dict[str, str] | os._Environ[str],
    task_dir: Path,
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
    try:
        _ctl(attach(app, namespace=namespace, task_dir=task_dir), argv)
    except DeployError as error:
        log(str(error))
        return 1
    return 0


def _ctl(deployment: Deployment, argv: Sequence[str]) -> None:
    """One of the accuracy checkers' hooks; anything else raises DeployError naming the usage."""
    match list(argv):
        case ["restart", name]:
            deployment.restart(name)
        case ["restart", name, "--every-pod"]:
            deployment.restart(name, every_pod=True)
        case ["flush", name]:
            deployment.flush(name)
        case ["stop"]:
            deployment.stop()
        case ["start"]:
            deployment.start()
        case _:
            raise DeployError(f"ctl {' '.join(argv)!r}: want {CTL_USAGE}")


def control(app: App[Any], argv: Sequence[str]) -> int:
    """Run `bench.py ctl ARGV` (restart, flush, stop, start) against a running sample."""
    return run_control(app, argv, environ=os.environ, task_dir=Path(sys.argv[0]).resolve().parent)
