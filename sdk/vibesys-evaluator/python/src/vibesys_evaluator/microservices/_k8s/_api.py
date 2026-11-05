"""The cluster as the deploy layer sees it: typed facts and the `KubeAPI` interface.

`KubectlAPI` implements it with kubectl; `FakeKube` in memory, for tests. Both
raise only `KubeError` (and its subclasses) for what the cluster refuses.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

WorkloadKind = Literal["Deployment", "StatefulSet"]


class KubeError(Exception):
    """The cluster refused or failed an operation; the message says which and why."""


class NotFoundError(KubeError):
    """The object does not exist."""


class ConflictError(KubeError):
    """A precondition failed: the object is not the one the caller meant (another UID)."""


@dataclass(frozen=True)
class Namespace:
    name: str
    uid: str
    labels: Mapping[str, str]
    created: float  # unix seconds, from the API server's creationTimestamp
    deleting: bool = False


@dataclass(frozen=True)
class Pod:
    name: str
    uid: str
    labels: Mapping[str, str]
    phase: str  # Pending, Running, Succeeded, Failed, Unknown
    ready: bool
    deleting: bool
    node: str | None
    restarts: int = 0
    waiting: str = ""  # a container's waiting reason, such as ImagePullBackOff
    exit_code: int | None = None  # the first container's, once terminated
    reason: str = ""  # the first container's termination reason, such as OOMKilled


@dataclass(frozen=True)
class Workload:
    kind: WorkloadKind
    name: str
    labels: Mapping[str, str]
    selector: Mapping[str, str]  # spec.selector.matchLabels
    replicas: int
    ready: int
    available: int
    updated: int
    generation: int
    observed: int


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str


@dataclass(frozen=True)
class PodStats:
    """One pod's counters from its node's kubelet summary."""

    cpu_seconds: float
    rx_bytes: int = 0
    tx_bytes: int = 0


class KubeAPI(Protocol):
    """What the deploy layer does to a cluster. Every call has a deadline of its implementation."""

    def create_namespace(self, name: str, labels: Mapping[str, str]) -> Namespace:
        """Create a namespace with its labels in one request."""
        ...

    def namespaces(self, selector: Mapping[str, str]) -> list[Namespace]:
        """The namespaces whose labels include every pair of selector."""
        ...

    def delete_namespace(self, name: str, *, uid: str) -> None:
        """Ask for deletion of the namespace if it still has this UID; do not wait for it.

        Raises ConflictError when the namespace has another UID, NotFoundError when gone.
        """
        ...

    def apply(self, namespace: str, objects: Sequence[Mapping[str, Any]]) -> None:
        """Server-side apply of the objects into the namespace."""
        ...

    def workloads(self, namespace: str) -> list[Workload]:
        """The Deployments and StatefulSets of the namespace."""
        ...

    def pods(self, namespace: str, selector: Mapping[str, str] | None = None) -> list[Pod]:
        """The pods of the namespace (whose labels include selector, when given)."""
        ...

    def delete_pods(self, namespace: str, names: Sequence[str], *, grace_secs: int) -> None:
        """Delete the pods; one already gone is not an error. Does not wait."""
        ...

    def scale(self, namespace: str, deployment: str, replicas: int) -> None:
        """Set a Deployment's replica count."""
        ...

    def exec(self, namespace: str, pod: str, argv: Sequence[str], *, timeout: float) -> ExecResult:
        """Run argv in the pod's first container; a nonzero exit is a result, not an error."""
        ...

    def put_files(
        self, namespace: str, pod: str, directory: str, files: Mapping[str, bytes]
    ) -> None:
        """Write files into a directory of the running pod, in the order given."""
        ...

    def get_files(self, namespace: str, pod: str, directory: str, into: Path) -> list[str]:
        """Copy every file of the pod's directory into `into`; return their relative names."""
        ...

    def logs(
        self, namespace: str, pod: str, *, tail: int | None = None, previous: bool = False
    ) -> str:
        """The pod's log (its previous container's with previous); '' when there is none."""
        ...

    def pod_stats(self, namespace: str, node: str) -> dict[str, PodStats]:
        """The kubelet counters of the namespace's pods on a node, by pod name."""
        ...
