"""The Kubernetes deploy layer, as the rest of the package uses it (private to the package).

The orchestrator uses `deployer(app)` (the `_private.deploy.Deployer` of an App) and
`control(app, argv)` (`bench.py ctl ...`). Underneath:

    cluster = Cluster(env=Environment.parse(os.environ, ...), kube=KubectlAPI(), ...)
    source = AppSource(app=app, task_dir=..., project_root=..., probe_plan=probe_plan(app.ready))
    with deploy(source, seed=PodCommand(argv=...), cluster=cluster) as deployment:
        run = deployment.run_pod(Role.PHASE, PodCommand(argv, {"input.binpb": ...}), timeout_secs=...)
        deployment.usage("phase-0")                     # per-pod CPU, diagnostics
    # the namespace is gone; after a signal its deletion is only requested

and `ctl(attach(source, namespace=..., cluster=cluster), argv)` for the accuracy hooks. Pods of
the package find their inputs in WORK_DIR and leave their outputs there.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys_evaluator._private.deploy import DeployError
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
from vibesys_evaluator.deploy._k8s._deployer import (
    KubeDeployer,
    KubePhaseRunner,
    control,
    deployer,
    probe_plan,
    run_control,
)
from vibesys_evaluator.deploy._k8s._deployment import (
    AppSource,
    Cluster,
    Deployment,
    Environment,
    PodCommand,
    PodRun,
    attach,
    ctl,
    deploy,
    sweep,
)
from vibesys_evaluator.deploy._k8s._fake import FakeBuilder, FakeKube, Outcome
from vibesys_evaluator.deploy._k8s._kubectl import KubectlAPI
from vibesys_evaluator.deploy._k8s._manifest import ALLOWED_KINDS, ManifestError, Rendered, render
from vibesys_evaluator.deploy._k8s._pods import SIZES, WORK_DIR, Role
from vibesys_evaluator.deploy._k8s._status import (
    CREATED_LABEL,
    JOB_LABEL,
    NAMESPACE_PREFIX,
    TASK_LABEL,
)

if TYPE_CHECKING:
    __all__ = [
        "ALLOWED_KINDS",
        "CREATED_LABEL",
        "JOB_LABEL",
        "NAMESPACE_PREFIX",
        "SIZES",
        "TASK_LABEL",
        "WORK_DIR",
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
        "KubeDeployer",
        "KubeError",
        "KubePhaseRunner",
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
        "Workload",
        "attach",
        "control",
        "ctl",
        "deploy",
        "deployer",
        "probe_plan",
        "render",
        "run_control",
        "sweep",
        "tree_digest",
    ]
