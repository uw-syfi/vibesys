"""The Kubernetes deploy layer of `microservices.k8s`: a thin wrapper over the Go core.

The logic (manifest rendering, image builds, readiness, the package's pods, fault actions,
teardown) is the Go package ``k8s`` behind ``vibesys-evaluator k8s <verb>``; this package builds the
requests and maps the replies (``_private/core.py`` runs the binary). The orchestrator uses
`deployer(app)` (the `_private.deploy.Deployer` of an App) and `control(app, argv)` (`bench.py ctl
...`); the fault actions are the public `k8s.attach(...)`.
"""

from __future__ import annotations

from vibesys_evaluator.microservices._k8s._deployer import (
    KubeDeployer,
    control,
    deployer,
    run_control,
)
from vibesys_evaluator.microservices._k8s._deployment import (
    Deployment,
    ExecResult,
    PodOutOfMemoryError,
    ReplacedPod,
    attach,
    probe_plan,
)

__all__ = [
    "Deployment",
    "ExecResult",
    "KubeDeployer",
    "PodOutOfMemoryError",
    "ReplacedPod",
    "attach",
    "control",
    "deployer",
    "probe_plan",
    "run_control",
]
