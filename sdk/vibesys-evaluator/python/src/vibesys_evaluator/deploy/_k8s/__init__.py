"""The Kubernetes deploy layer, as the rest of the package uses it (private to the package).

The orchestrator (`vibesys_evaluator.main`) calls, in order:

    env = Environment.parse(os.environ, work_dir=scratch)
    cluster = Cluster(env=env, kube=KubectlAPI(), builder=BuildctlBuilder(env.builder, clock),
                      clock=clock, log=log)
    source = AppSource(app=app, task_dir=..., project_root=..., probe_plan=...)
    with deploy(source, seed=PodCommand(argv, {"dataset.pickle": ...}), cluster=cluster) as deployment:
        run = deployment.run_pod(Role.PHASE, PodCommand(argv, {"phase-input.binpb": ...}),
                                 timeout_secs=...)      # outputs in run.outputs
        deployment.usage("phase-0")                     # per-pod CPU, diagnostics
    # the namespace is gone; on Terminated (TerminationGuard) the deletion is only requested

and `ctl(attach(source, namespace=..., cluster=cluster), argv)` for the accuracy hooks. Pods of the
package read their inputs from IN_DIR and leave their outputs in OUT_DIR.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys_evaluator.deploy._k8s._api import (
    ConflictError,
    ExecResult,
    KubeAPI,
    KubeError,
    Namespace,
    NotFoundError,
    Pod,
    PodStats,
    Workload,
)
from vibesys_evaluator.deploy._k8s._build import (
    Build,
    BuildctlBuilder,
    Builder,
    BuildError,
    tree_digest,
)
from vibesys_evaluator.deploy._k8s._clock import Clock, FakeClock, SystemClock
from vibesys_evaluator.deploy._k8s._deployment import (
    AppSource,
    Cluster,
    DeployError,
    Deployment,
    Environment,
    PodCommand,
    PodRun,
    Terminated,
    TerminationGuard,
    attach,
    deploy,
    sweep,
)
from vibesys_evaluator.deploy._k8s._fake import FakeBuilder, FakeKube, Outcome
from vibesys_evaluator.deploy._k8s._kubectl import KubectlAPI
from vibesys_evaluator.deploy._k8s._manifest import ALLOWED_KINDS, ManifestError, Rendered, render
from vibesys_evaluator.deploy._k8s._pods import IN_DIR, OUT_DIR, SIZES, Role
from vibesys_evaluator.deploy._k8s._status import (
    CREATED_LABEL,
    JOB_LABEL,
    NAMESPACE_PREFIX,
    TASK_LABEL,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "ALLOWED_KINDS",
    "CREATED_LABEL",
    "IN_DIR",
    "JOB_LABEL",
    "NAMESPACE_PREFIX",
    "OUT_DIR",
    "SIZES",
    "TASK_LABEL",
    "AppSource",
    "Build",
    "BuildError",
    "BuildctlBuilder",
    "Builder",
    "Clock",
    "Cluster",
    "ConflictError",
    "DeployError",
    "Deployment",
    "Environment",
    "ExecResult",
    "FakeBuilder",
    "FakeClock",
    "FakeKube",
    "KubeAPI",
    "KubeError",
    "KubectlAPI",
    "ManifestError",
    "Namespace",
    "NotFoundError",
    "Outcome",
    "Pod",
    "PodCommand",
    "PodRun",
    "PodStats",
    "Rendered",
    "Role",
    "SystemClock",
    "Terminated",
    "TerminationGuard",
    "Workload",
    "attach",
    "ctl",
    "deploy",
    "render",
    "sweep",
    "tree_digest",
]

CTL_USAGE = "ctl restart DEPLOYMENT [--every-pod] | flush CACHE | stop | start"


def ctl(deployment: Deployment, argv: Sequence[str]) -> None:
    """Run one of the accuracy checkers' hooks.

    `restart DEPLOYMENT [--every-pod]`, `flush CACHE`, `stop` or `start`; anything else
    raises DeployError naming the usage.
    """
    args = list(argv)
    match args:
        case ["restart", name]:
            deployment.restart([name])
        case ["restart", name, "--every-pod"]:
            deployment.restart([name], every_pod=True)
        case ["flush", name]:
            deployment.flush(name)
        case ["stop"]:
            deployment.stop()
        case ["start"]:
            deployment.start()
        case _:
            raise DeployError(f"ctl {' '.join(args)!r}: want {CTL_USAGE}")
