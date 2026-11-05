"""The system under test on Kubernetes.

A task describes its application with one `App`; the package builds its
images, deploys it into a fresh namespace of the cluster that vibesys-cli's
`kubernetes` resource gives (`KUBECONFIG`, `VIBESYS_K8S_REGISTRY`,
`VIBESYS_K8S_BUILDER`), seeds it, waits until it serves, runs every phase in
a pod of that namespace and deletes the namespace on every exit path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Literal

from vibesys_evaluator._private.typevars import D
from vibesys_evaluator.microservices._k8s import _validate

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vibesys_evaluator.microservices import Step

__all__ = ["App"]


@dataclass(frozen=True, kw_only=True)
class App(Generic[D]):
    """An application on Kubernetes: what to build, what to apply, how to tell it serves.

    - `name`: the task's application name (lowercase letters, digits and
      dashes). It labels the namespaces of this task, so the sweep of stale
      namespaces left by killed samples only ever deletes this task's.
    - `manifest`: path of the Kubernetes manifest, relative to the task
      directory. `${NAMESPACE}` and every key of `images` are substituted; any
      other `${...}` is an error. Only namespaced application kinds are
      accepted (ConfigMap, Secret, Service, ServiceAccount,
      PersistentVolumeClaim, Deployment, StatefulSet, PodDisruptionBudget).
    - `images`: manifest variable to candidate directory (relative to the
      project root), each built with its own Dockerfile and tagged by a digest
      of the directory, so an unchanged directory is not rebuilt.
    - `gateway`: base URL of every request, `http://<service>:<port>`, a
      Service of the manifest.
    - `ready`: requests that must succeed, twice per ready endpoint of the
      gateway Service on fresh connections, once every Deployment and
      StatefulSet has all its replicas available.
    - `data_tier`: a label selector (`key=value[,key=value]`). The objects it
      selects and every non-workload object are applied first and waited for;
      then `seed` runs; then the remaining workloads are applied. Empty: the
      whole manifest is applied and serves before `seed` runs.
    - `seed`: called with the dataset in a Job of the task image, before the
      measured phases (and, with `data_tier`, before the application starts).
    - `restartable`: Deployments the accuracy hooks may restart, stop and
      start (`ctl restart DEPLOYMENT | stop | start`).
    - `caches`: cache Deployments the hooks may empty in place
      (`ctl flush CACHE`), with their protocol.
    """

    name: str
    manifest: str
    images: Mapping[str, str]
    gateway: str
    ready: Sequence[Step]
    data_tier: str = ""
    seed: Callable[[D], None] | None = None
    restartable: Sequence[str] = ()
    caches: Mapping[str, Literal["memcached", "redis"]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Check every field and freeze the collections; a bad field raises ValueError naming it."""
        _validate.check_app(self)
        object.__setattr__(self, "images", MappingProxyType(dict(self.images)))
        object.__setattr__(self, "ready", tuple(self.ready))
        object.__setattr__(self, "restartable", tuple(self.restartable))
        object.__setattr__(self, "caches", MappingProxyType(dict(self.caches)))
