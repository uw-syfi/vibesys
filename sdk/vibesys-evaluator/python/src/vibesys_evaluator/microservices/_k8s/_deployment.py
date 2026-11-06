"""A deployed `k8s.App`: what the fault actions and the phase runner call, over the Go core.

Every method is one call of `vibesys-evaluator k8s <verb>` (`_private/core.py`); the logic is in the
Go package `k8s`. A `Deployment` is a handle: the core keeps no state between calls, so the handle
carries the `target` message `k8s deploy` returned (the app, the namespace, its UID, the task
image) and gives it back with every verb.
"""

from __future__ import annotations

import io
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vibesys_evaluator._private import core
from vibesys_evaluator._private.files import DelimitedWriter
from vibesys_evaluator._private.http import step_message
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator.microservices._k8s import _validate
from vibesys_evaluator.microservices._private.deploy import DeployError

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vibesys_evaluator._private.http import Step
    from vibesys_evaluator.microservices.k8s import App

__all__ = [
    "NAMESPACE_VARIABLE",
    "WORK_DIR",
    "Deployment",
    "ExecResult",
    "PodOutOfMemoryError",
    "ReplacedPod",
    "attach",
    "probe_plan",
]

NAMESPACE_VARIABLE = "VIBESYS_EVALUATOR_NAMESPACE"  # the sample `ctl` acts on
#: A package pod's work directory (the Go package's `k8s.WorkDir`).
WORK_DIR = "/vse/work"
#: How long the core gets to delete a namespace after SIGTERM before it is killed.
TERMINATE_GRACE_SECS = 20.0
_LIMIT = re.compile(r"OOMKilled at its (\S+) limit")


class PodOutOfMemoryError(DeployError):
    """A pod of the package was OOMKilled; ``limit`` is its memory limit (``4Gi``)."""

    def __init__(self, message: str, *, limit: str) -> None:
        super().__init__(message)
        self.limit = limit


@dataclass(frozen=True)
class ExecResult:
    """What a command run in a pod answered: its pod, exit status and combined output."""

    pod: str
    exit_code: int
    output: str


@dataclass(frozen=True)
class ReplacedPod:
    """A pod a restart deleted; ``uid`` identifies it for `Deployment.wait_ready`."""

    deployment: str
    pod: str
    uid: str


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


def app_message(app: App[Any]) -> dict[str, Any]:
    """The `K8sApp` message of an App."""
    return {
        "name": app.name,
        "manifest": app.manifest,
        "images": dict(app.images),
        "gateway": app.gateway,
        "data_tier": app.data_tier,
        "has_seed": app.seed is not None,
        "restartable": list(app.restartable),
        "caches": {name: f"K8S_CACHE_KIND_{kind.upper()}" for name, kind in app.caches.items()},
    }


def call(verb: str, request: Mapping[str, Any], *, grace: bool = False) -> dict[str, Any]:
    """Run ``vibesys-evaluator k8s VERB``; every failure is a `DeployError`.

    ``grace``: a verb that owns cluster state is given time to clean up when this wait is cut
    short by a signal.
    """
    try:
        return core.call(
            "k8s", verb, request, terminate_grace_secs=TERMINATE_GRACE_SECS if grace else 0.0
        )
    except core.CoreDomainError as error:
        if error.kind == "out_of_memory":
            limit = _LIMIT.search(error.message)
            raise PodOutOfMemoryError(
                error.message, limit=limit.group(1) if limit else "its"
            ) from None
        raise DeployError(error.message) from None
    except core.CoreError as error:
        raise DeployError(f"k8s {verb}: the Go core failed: {error}") from None


class Deployment:
    """An App deployed in its namespace, and the fault actions an accuracy check applies to it.

    Get one with `attach` (the namespace a running sample deployed). The actions act on the
    Deployments the App lists in ``restartable`` and ``caches``, and nothing else.
    """

    def __init__(
        self, app: App[Any], target: Mapping[str, Any], *, scratch: object | None = None
    ) -> None:
        """Wrap the ``target`` message of a deploy (private: use `attach`).

        ``scratch`` is a temporary directory the handle keeps alive, and removes with it.
        """
        self._app = app
        self._target = dict(target)
        self._scratch = scratch
        self._pods = 0

    @property
    def _gateway_url(self) -> str:
        """The base URL of the app's gateway as the phase pods reach it."""
        return _validate.parse_gateway(self._app.gateway).url

    @property
    def namespace(self) -> str:
        """The Kubernetes namespace of the sample."""
        return str(self._target["namespace"])

    def restart(
        self, *deployments: str, every_pod: bool = False, wait: bool = True
    ) -> list[ReplacedPod]:
        """Delete one pod (the first by name) or, with ``every_pod``, every pod of each Deployment.

        With ``wait`` (the default) it returns when no deleted pod remains, every workload serves
        with all its replicas and the probes through the gateway pass. Without it, it returns the
        deleted pods at once: pass them to `wait_ready`.

        Raises:
            DeployError: A Deployment is not in ``restartable`` or ``caches``, or does not serve
                again in time.
        """
        reply = call(
            "restart",
            {
                "target": self._target,
                "deployments": list(deployments),
                "every_pod": every_pod,
                "no_wait": not wait,
            },
        )
        return [
            ReplacedPod(r.get("deployment", ""), r.get("pod", ""), r.get("uid", ""))
            for r in reply.get("replaced", [])
        ]

    def exec(
        self,
        argv: Sequence[str],
        *,
        deployment: str | None = None,
        pod: str | None = None,
        all_pods: bool = False,
        timeout_secs: float = 60.0,
        check: bool = False,
    ) -> list[ExecResult]:
        """Run a command in a pod or in the pods of a Deployment; return each pod's answer.

        Name a ``deployment`` (its first pod by name, or every live pod with ``all_pods``) or a
        ``pod``. A nonzero exit status is a result; ``check=True``
        raises on it instead.

        Raises:
            DeployError: The target is not in ``restartable`` or ``caches``, or ``check`` and a
                command exited nonzero.
        """
        reply = call(
            "exec",
            {
                "target": self._target,
                "deployment": deployment or "",
                "pod": pod or "",
                "all_pods": all_pods,
                "argv": list(argv),
                "timeout_secs": timeout_secs,
            },
        )
        results = [
            ExecResult(r.get("pod", ""), int(r.get("exit_code", 0)), r.get("output", ""))
            for r in reply.get("results", [])
        ]
        if check:
            failed = next((r for r in results if r.exit_code != 0), None)
            if failed is not None:
                msg = f"exec {' '.join(argv)!r} in {failed.pod} exited {failed.exit_code}: {failed.output.strip()[-300:]}"
                raise DeployError(msg)
        return results

    def wait_ready(
        self,
        deployments: Sequence[str] = (),
        *,
        replaced: Sequence[ReplacedPod | str] = (),
        timeout_secs: float = 300.0,
        probe: bool = True,
    ) -> None:
        """Wait until the app serves again, then probe through the gateway.

        Every workload (of ``deployments``, else all) must serve with all its replicas, none of
        them a pod of ``replaced`` (pods or their UIDs).

        Raises:
            DeployError: It does not serve within ``timeout_secs``.
        """
        call(
            "wait",
            {
                "target": self._target,
                "deployments": list(deployments),
                "replaced_uids": [r.uid if isinstance(r, ReplacedPod) else r for r in replaced],
                "timeout_secs": timeout_secs,
                "no_probe": not probe,
            },
        )

    def flush(self, cache: str) -> None:
        """Empty a cache of ``caches`` in place, on every pod of its Deployment, then probe."""
        call("flush", {"target": self._target, "cache": cache})

    def stop(self) -> None:
        """Scale every ``restartable`` Deployment to 0 and wait until their pods are gone."""
        call("stop", {"target": self._target})

    def start(self) -> None:
        """Scale the ``restartable`` Deployments back to the manifest's replicas; wait; probe."""
        call("start", {"target": self._target})

    # -- what the phase runner uses (not part of the public API) ------------------------------

    def _run_pod(
        self,
        role: str,
        argv: Sequence[str],
        *,
        inputs: Mapping[str, Path],
        timeout_secs: float,
        outputs: Path,
    ) -> dict[str, Any]:
        self._pods += 1
        return call(
            "run-pod",
            {
                "target": self._target,
                "role": f"K8S_ROLE_{role.upper()}",
                "command": {
                    "argv": list(argv),
                    "input_files": {name: str(path) for name, path in inputs.items()},
                },
                "timeout_secs": timeout_secs,
                "outputs_dir": str(outputs),
                "pod_name": f"vse-{role}-{self._pods}",
            },
        )

    def _usage(self, label: str) -> None:
        """Diagnostics only: per-pod CPU is logged by the core and saved in the artifacts."""
        try:
            call("usage", {"target": self._target, "label": label})
        except DeployError as error:
            print(f"vibesys-evaluator: pod usage {label}: {error}", file=sys.stderr, flush=True)

    def _teardown(self, *, fast: bool, failed: bool) -> None:
        call("teardown", {"target": self._target, "fast": fast, "failed": failed}, grace=True)


def attach(
    app: App[Any],
    *,
    namespace: str | None = None,
    task_dir: str | os.PathLike[str] | None = None,
) -> Deployment:
    """Act on the namespace a running sample deployed (never deletes it).

    Args:
        app: The task's system under test.
        namespace: The sample's namespace; ``$VIBESYS_EVALUATOR_NAMESPACE`` (the ``namespace.txt`` of
            the sample's artifacts) when None.
        task_dir: The task directory (the manifest's base); the directory of the running script
            when None.

    Raises:
        DeployError: No namespace is given, or the cluster has no such namespace of this app.
    """
    chosen = namespace if namespace is not None else os.environ.get(NAMESPACE_VARIABLE, "")
    if not chosen:
        msg = (
            f"{NAMESPACE_VARIABLE} is not set: name the sample's namespace "
            "(namespace.txt in its artifacts)"
        )
        raise DeployError(msg)
    directory = Path(task_dir) if task_dir is not None else Path(sys.argv[0]).resolve().parent
    scratch = tempfile.TemporaryDirectory(prefix="vse-ctl-")
    plan = Path(scratch.name) / "probe.binpb"
    plan.write_bytes(probe_plan(app.ready))
    target = {
        "app": app_message(app),
        "task_dir": str(directory),
        "namespace": chosen,
        "work_dir": scratch.name,
        "probe_plan_file": str(plan),
    }
    # Fail now, not at the first action: the core checks the namespace belongs to the app.
    checked = call("attach", {"target": target})
    return Deployment(app, checked.get("target", target), scratch=scratch)
