"""Pure decisions of the deploy layer: names, labels, the sweep, readiness, restart victims, usage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from vibesys_evaluator.microservices._k8s._api import Namespace, Pod, PodStats, Workload

# Namespaces of this package's tasks are hk8s-<slug>-<token>: the clusters it was built
# for keep per-task namespaces under this prefix. A namespace carries the task label
# (the App's name), its creation time and the vibesys-cli job that made it.
NAMESPACE_PREFIX = "hk8s-"
TASK_LABEL = "vibesys.dev/evaluator-task"
CREATED_LABEL = "vibesys.dev/evaluator-created"  # unix seconds
JOB_LABEL = "vibesys.dev/evaluator-job"
SWEEP_MARGIN_SECS = 600
_SLUG_CHARS = 24
# Waiting reasons a pod does not recover from by itself: fail at once instead of at the deadline.
FATAL_WAITING = frozenset({"InvalidImageName", "CreateContainerConfigError", "ErrImageNeverPull"})


def slug(app_name: str) -> str:
    """The App's name cut to fit a namespace name, without a trailing dash."""
    return app_name[:_SLUG_CHARS].rstrip("-")


def namespace_prefix(app_name: str) -> str:
    return f"{NAMESPACE_PREFIX}{slug(app_name)}-"


def namespace_name(app_name: str, token: str) -> str:
    return namespace_prefix(app_name) + token


def namespace_labels(app_name: str, *, now: float, job: str) -> dict[str, str]:
    return {TASK_LABEL: app_name, CREATED_LABEL: str(int(now)), JOB_LABEL: job}


_TOKEN = re.compile(r"^[0-9a-z]{1,16}$")  # what follows the prefix in a name namespace_name made
_JOB = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$")


def job_label(artifacts_dir: str, fallback: str) -> str:
    """The vibesys-cli job id: the parent of $VIBESYS_ARTIFACTS_DIR (.../<job>/<step>), else fallback."""
    *parents, _step = [part for part in artifacts_dir.split("/") if part] or [""]
    name = parents[-1] if parents else ""
    return name if _JOB.match(name) else fallback


def namespace_age(namespace: Namespace, now: float) -> float:
    created = namespace.labels.get(CREATED_LABEL, "")
    stamp = int(created) if created.isdigit() else namespace.created
    # The younger of the label's and the API server's age: neither our clock nor the
    # server's running ahead makes a namespace stale early.
    return now - max(stamp, namespace.created)


def stale(
    namespaces: Iterable[Namespace], *, app_name: str, now: float, ttl: float
) -> list[Namespace]:
    """This task's namespaces that no live sample can own.

    Labelled with its name, named with its prefix and a token, and older than ttl (the
    longest a job may run plus a margin).
    """
    prefix = namespace_prefix(app_name)
    return sorted(
        (
            ns
            for ns in namespaces
            if ns.labels.get(TASK_LABEL) == app_name
            and ns.name.startswith(prefix)
            and _TOKEN.match(ns.name[len(prefix) :])
            and not ns.deleting
            and namespace_age(ns, now) > ttl
        ),
        key=lambda ns: ns.name,
    )


def selects(selector: Mapping[str, str], labels: Mapping[str, str]) -> bool:
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def _pod_reason(pod: Pod) -> str:
    if pod.waiting:
        return pod.waiting
    if pod.deleting:
        return "terminating"
    return pod.phase if pod.phase != "Running" else "not ready"


def unready(
    workloads: Sequence[Workload],
    pods: Sequence[Pod],
    *,
    names: Iterable[str] | None = None,
    replaced: frozenset[str] = frozenset(),
) -> list[str]:
    """Why each workload (of names, when given) does not serve with all its replicas; [] when all do.

    A workload serves when its controller has seen its spec, exactly `replicas` of its pods
    exist that are neither terminating nor among the replaced UIDs, all of them Ready, and
    its status counts every replica ready, available and updated.
    """
    wanted = None if names is None else set(names)
    by_name = {w.name: w for w in workloads}
    reasons = [
        f"{name}: no such Deployment or StatefulSet"
        for name in sorted(wanted or ())
        if name not in by_name
    ]
    for workload in sorted(workloads, key=lambda w: (w.kind, w.name)):
        if wanted is not None and workload.name not in wanted:
            continue
        own = [p for p in pods if selects(workload.selector, p.labels)]
        live = [p for p in own if not p.deleting and p.uid not in replaced]
        ready = [p for p in live if p.ready]
        counts = (workload.ready, workload.available, workload.updated)
        if (
            workload.observed >= workload.generation
            and len(live) == len(own) == workload.replicas
            and len(ready) == workload.replicas
            and all(c == workload.replicas for c in counts)
        ):
            continue
        problems = sorted({f"{p.name}: {_pod_reason(p)}" for p in own if p not in ready})
        detail = f" ({'; '.join(problems)})" if problems else ""
        reasons.append(
            f"{workload.kind.lower()}/{workload.name}: {len(ready)}/{workload.replicas} "
            f"ready{detail}"
        )
    return reasons


def fatal(pods: Sequence[Pod]) -> list[str]:
    return sorted(f"{p.name}: {p.waiting}" for p in pods if p.waiting in FATAL_WAITING)


def victims(pods: Sequence[Pod], *, every: bool) -> list[Pod]:
    """The pods a restart deletes: every live one, or the first by name."""
    live = sorted((p for p in pods if not p.deleting), key=lambda p: p.name)
    return live if every else live[:1]


@dataclass(frozen=True)
class Usage:
    """CPU seconds and network bytes of one pod since it started, and its workload."""

    workload: str
    cpu_seconds: float
    rx_bytes: int
    tx_bytes: int


def usage(
    pods: Sequence[Pod], workloads: Sequence[Workload], stats: Mapping[str, PodStats]
) -> dict[str, Usage]:
    """Join kubelet counters to the pods and their workloads, by pod name."""
    owner = {
        p.name: next((w.name for w in workloads if selects(w.selector, p.labels)), "") for p in pods
    }
    return {
        name: Usage(
            workload=owner.get(name, ""),
            cpu_seconds=round(item.cpu_seconds, 3),
            rx_bytes=item.rx_bytes,
            tx_bytes=item.tx_bytes,
        )
        for name, item in sorted(stats.items())
    }


def usage_delta(before: Mapping[str, Usage], after: Mapping[str, Usage]) -> dict[str, float]:
    """CPU seconds each pod used between two snapshots (a pod new since `before` counts from 0)."""
    return {
        name: round(item.cpu_seconds - (before[name].cpu_seconds if name in before else 0.0), 3)
        for name, item in after.items()
    }
