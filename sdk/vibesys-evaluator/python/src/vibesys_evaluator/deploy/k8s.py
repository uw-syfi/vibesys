"""The system under test on Kubernetes.

Interface stub owned by the k8s deploy work package: the fields are the
public API; the implementation lives in ``deploy._k8s``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Generic, Literal

from vibesys_evaluator._private.typevars import D

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from vibesys_evaluator.service import Step

__all__ = ["App"]


@dataclass(frozen=True, kw_only=True)
class App(Generic[D]):
    """An application deployed from a manifest into a fresh namespace per sample.

    Attributes:
        name: The task's name: namespaces ``<name>-<hex>`` and labels.
        manifest: The manifest, relative to the task directory; ``${NAMESPACE}``
            and the image variables are substituted.
        images: Manifest variable to candidate directory (below the project
            root), built and pushed per sample.
        gateway: The base URL the phases send to, inside the namespace.
        ready: Steps that must succeed through the gateway before the phases.
        data_tier: A label selector applied and waited for before ``seed``.
        seed: Writes the dataset into the system, in a Job of the task image.
        restartable: Deployments the accuracy hooks may restart or stop.
        caches: Caches the accuracy hooks may flush, by kind.
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
