"""`FakeKube` and `FakeBuilder`: an in-memory cluster and image builder for tests.

The fake is as strict as a cluster: namespaces have UIDs and refuse a deletion whose
precondition names another, objects go only into a live namespace, controllers keep each
workload's replica count of pods, pods take time (on the injected FakeClock) to start and
become Ready, a deleted pod lingers for its grace period, and exec, file copies and logs
need a running pod. A pod of the package runs the wrapper protocol of `_pods`: its
command, looked up in `programs` by argv[0], runs once the ready marker arrives, and its
exit marker and outputs appear when it is done.
"""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from vibesys_evaluator.microservices._k8s import _build, _pods
from vibesys_evaluator.microservices._k8s._api import (
    ConflictError,
    ExecResult,
    KubeError,
    Namespace,
    NotFoundError,
    Pod,
    PodStats,
    Workload,
    WorkloadKind,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys_evaluator.microservices._k8s._clock import FakeClock


@dataclass(frozen=True)
class Outcome:
    """What a fake pod's command does.

    Its exit status, the files it leaves in WORK_DIR, its log, and how long it runs. crash:
    the container dies instead (no exit marker, pod Failed, exit 137), with the termination
    reason crash_reason (OOMKilled when the kernel killed it at its memory limit).
    """

    exit_code: int = 0
    outputs: Mapping[str, bytes] = field(default_factory=dict)
    log: str = ""
    secs: float = 1.0
    crash: bool = False
    crash_reason: str = "Error"


Program = Callable[[Sequence[str], Mapping[str, bytes]], Outcome]


@dataclass
class _Pod:
    name: str
    uid: str
    labels: dict[str, str]
    created: float
    start_secs: float
    owner: str  # workload or Job name
    argv: tuple[str, ...] = ()
    files: dict[str, bytes] = field(default_factory=dict)
    deleted_at: float | None = None
    grace: float = 0.0
    log: str = ""
    done_at: float | None = None
    outcome: Outcome | None = None
    finished: bool = False
    never_ready: str = ""  # a waiting reason that keeps it from ever being Ready
    node: str = "node-a"


@dataclass
class _Workload:
    kind: WorkloadKind
    name: str
    labels: dict[str, str]
    selector: dict[str, str]
    template_labels: dict[str, str]
    replicas: int
    generation: int = 1


@dataclass
class _Namespace:
    name: str
    uid: str
    labels: dict[str, str]
    created: float
    deleted_at: float | None = None
    objects: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    workloads: dict[str, _Workload] = field(default_factory=dict)
    pods: dict[str, _Pod] = field(default_factory=dict)
    jobs: set[str] = field(default_factory=set)
    flushed: list[str] = field(default_factory=list)


class FakeKube:
    """An in-memory KubeAPI on a FakeClock.

    Tests read `calls`, `namespace_objects()` and `flushed`, set `start_secs`/`broken` per
    workload and `programs` per command, and schedule cluster events with `at_call`.
    """

    NAMESPACE_DELETE_SECS = 5.0

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[str] = []
        self.programs: dict[str, Program] = {}
        self.start_secs: dict[str, float] = {}
        self.broken: dict[str, str] = {}  # workload -> waiting reason of its pods
        self._namespaces: dict[str, _Namespace] = {}
        self._serial = 0
        self._actions: dict[int, Callable[[], object]] = {}

    # -- test controls ------------------------------------------------------------------------

    def at_call(self, call: int, action: Callable[[], object]) -> None:
        """Run action when the call-th API call (1-based) starts, before it takes effect.

        It may change the cluster (another job's doing) or raise (a signal, a failure).
        """
        self._actions[call] = action

    def interrupt_at(self, call: int, error: BaseException) -> None:
        """Raise error when the call-th API call starts."""

        def interrupt() -> None:
            raise error

        self.at_call(call, interrupt)

    def add_namespace(self, name: str, labels: Mapping[str, str], *, created: float) -> Namespace:
        """A namespace some other job made (for sweep tests)."""
        ns = _Namespace(name=name, uid=self._uid(), labels=dict(labels), created=created)
        self._namespaces[name] = ns
        return self._view(ns)

    def recreate(self, name: str) -> Namespace:
        """Delete and recreate a namespace at once with the same labels: a new UID."""
        old = self._namespaces[name]
        return self.add_namespace(name, old.labels, created=self.clock.now())

    def live_namespaces(self) -> list[str]:
        self._purge()
        return sorted(name for name, ns in self._namespaces.items() if ns.deleted_at is None)

    def all_namespaces(self) -> list[str]:
        self._purge()
        return sorted(self._namespaces)

    def namespace_objects(self, namespace: str) -> dict[tuple[str, str], dict[str, Any]]:
        return copy.deepcopy(self._namespaces[namespace].objects)

    def flushed(self, namespace: str) -> list[str]:
        return list(self._namespaces[namespace].flushed)

    # -- internals ----------------------------------------------------------------------------

    def _uid(self) -> str:
        self._serial += 1
        return f"uid-{self._serial:04d}"

    def _call(self, name: str) -> None:
        self.calls.append(name)
        action = self._actions.pop(len(self.calls), None)
        if action is not None:
            action()

    def _purge(self) -> None:
        now = self.clock.now()
        for name in [
            n
            for n, ns in self._namespaces.items()
            if ns.deleted_at is not None and now >= ns.deleted_at + self.NAMESPACE_DELETE_SECS
        ]:
            del self._namespaces[name]
        for ns in self._namespaces.values():
            self._reconcile(ns)

    def _view(self, ns: _Namespace) -> Namespace:
        return Namespace(
            name=ns.name,
            uid=ns.uid,
            labels=dict(ns.labels),
            created=ns.created,
            deleting=ns.deleted_at is not None,
        )

    def _live(self, namespace: str) -> _Namespace:
        self._purge()
        ns = self._namespaces.get(namespace)
        if ns is None:
            raise NotFoundError(f'Error from server (NotFound): namespaces "{namespace}" not found')
        return ns

    def _writable(self, namespace: str) -> _Namespace:
        ns = self._live(namespace)
        if ns.deleted_at is not None:
            raise KubeError(f"namespace {namespace} is being terminated")
        return ns

    def _new_pod(
        self,
        ns: _Namespace,
        *,
        name: str,
        labels: Mapping[str, str],
        owner: str,
        argv: tuple[str, ...] = (),
    ) -> _Pod:
        pod = _Pod(
            name=name,
            uid=self._uid(),
            labels=dict(labels),
            created=self.clock.now(),
            start_secs=self.start_secs.get(owner, 3.0),
            owner=owner,
            argv=argv,
            never_ready=self.broken.get(owner, ""),
        )
        ns.pods[name] = pod
        return pod

    def _reconcile(self, ns: _Namespace) -> None:
        now = self.clock.now()
        for name in [
            n
            for n, p in ns.pods.items()
            if p.deleted_at is not None and now >= p.deleted_at + p.grace
        ]:
            del ns.pods[name]
        for workload in ns.workloads.values():
            live = [
                p for p in ns.pods.values() if p.owner == workload.name and p.deleted_at is None
            ]
            for pod in sorted(live, key=lambda p: p.name)[workload.replicas :]:
                pod.deleted_at, pod.grace = now, 1.0
            for _ in range(workload.replicas - len(live)):
                self._new_pod(
                    ns,
                    name=f"{workload.name}-{self._uid()[4:]}",
                    labels=workload.template_labels,
                    owner=workload.name,
                )
        for pod in ns.pods.values():
            if (
                pod.outcome is not None
                and pod.done_at is not None
                and now >= pod.done_at
                and pod.outcome.crash
            ):
                pod.finished = True

    def _running(self, pod: _Pod) -> bool:
        return (
            pod.deleted_at is None
            and not pod.finished
            and not pod.never_ready
            and self.clock.now() >= pod.created + pod.start_secs
        )

    def _pod_view(self, pod: _Pod) -> Pod:
        now = self.clock.now()
        started = now >= pod.created + pod.start_secs
        exit_code = None
        reason = ""
        if pod.finished:
            phase = (
                "Failed"
                if pod.outcome is None or pod.outcome.crash or pod.outcome.exit_code
                else "Succeeded"
            )
            exit_code = 137 if pod.outcome is None or pod.outcome.crash else pod.outcome.exit_code
            if pod.outcome is not None and pod.outcome.crash:
                reason = pod.outcome.crash_reason
        elif pod.never_ready in ("ImagePullBackOff", "ErrImagePull", "InvalidImageName"):
            phase = "Pending"
        else:
            phase = "Running" if started else "Pending"
        waiting = pod.never_ready or ("" if started else "ContainerCreating")
        return Pod(
            name=pod.name,
            uid=pod.uid,
            labels=dict(pod.labels),
            phase=phase,
            ready=phase == "Running" and not pod.never_ready,
            deleting=pod.deleted_at is not None,
            node=pod.node,
            waiting=waiting if phase != "Failed" else "",
            exit_code=exit_code,
            reason=reason,
        )

    def _pod(self, namespace: str, name: str) -> _Pod:
        pod = self._live(namespace).pods.get(name)
        if pod is None:
            raise NotFoundError(f'Error from server (NotFound): pods "{name}" not found')
        if not self._running(pod):
            raise KubeError(f"Error from server: pod {name} is not running")
        return pod

    # -- KubeAPI ------------------------------------------------------------------------------

    def create_namespace(self, name: str, labels: Mapping[str, str]) -> Namespace:
        self._call("create_namespace")
        self._purge()
        if name in self._namespaces:
            raise KubeError(
                f'Error from server (AlreadyExists): namespaces "{name}" already exists'
            )
        return self.add_namespace(name, labels, created=self.clock.now())

    def namespaces(self, selector: Mapping[str, str]) -> list[Namespace]:
        self._call("namespaces")
        self._purge()
        return [
            self._view(ns)
            for ns in self._namespaces.values()
            if all(ns.labels.get(k) == v for k, v in selector.items())
        ]

    def delete_namespace(self, name: str, *, uid: str) -> None:
        self._call("delete_namespace")
        ns = self._live(name)
        if ns.uid != uid:
            raise ConflictError(
                f"Error from server (Conflict): Precondition failed: UID in precondition: {uid}, "
                f"UID in object meta: {ns.uid}"
            )
        if ns.deleted_at is None:
            ns.deleted_at = self.clock.now()

    def apply(self, namespace: str, objects: Sequence[Mapping[str, Any]]) -> None:
        self._call("apply")
        ns = self._writable(namespace)
        for raw in objects:
            obj = copy.deepcopy(dict(raw))
            kind, name = obj["kind"], obj["metadata"]["name"]
            if obj["metadata"].get("namespace", namespace) != namespace:
                raise KubeError(
                    f"{kind} {name}: the namespace of the object does not match {namespace}"
                )
            ns.objects[(kind, name)] = obj
            spec = obj.get("spec", {})
            if kind in ("Deployment", "StatefulSet"):
                template = spec["template"]["metadata"]["labels"]
                selector = spec["selector"]["matchLabels"]
                if not all(template.get(k) == v for k, v in selector.items()):
                    raise KubeError(f"{kind} {name}: selector does not match template labels")
                previous = ns.workloads.get(name)
                ns.workloads[name] = _Workload(
                    kind="Deployment" if kind == "Deployment" else "StatefulSet",
                    name=name,
                    labels=dict(obj["metadata"].get("labels") or {}),
                    selector=dict(selector),
                    template_labels=dict(template),
                    replicas=int(spec.get("replicas", 1)),
                    generation=previous.generation + 1 if previous else 1,
                )
            elif kind == "Job" and name not in ns.jobs:
                ns.jobs.add(name)
                container = spec["template"]["spec"]["containers"][0]
                labels = {**spec["template"]["metadata"].get("labels", {}), "job-name": name}
                # The wrapper: ["sh", "-c", WRAPPER, "vse-pod", *argv].
                argv = tuple(container["command"][4:])
                self._new_pod(
                    ns, name=f"{name}-{self._uid()[4:]}", labels=labels, owner=name, argv=argv
                )
        self._reconcile(ns)

    def workloads(self, namespace: str) -> list[Workload]:
        self._call("workloads")
        ns = self._live(namespace)
        result = []
        for w in ns.workloads.values():
            live = [p for p in ns.pods.values() if p.owner == w.name and p.deleted_at is None]
            ready = sum(1 for p in live if self._pod_view(p).ready)
            result.append(
                Workload(
                    kind=w.kind,
                    name=w.name,
                    labels=dict(w.labels),
                    selector=dict(w.selector),
                    replicas=w.replicas,
                    ready=ready,
                    available=ready,
                    updated=len(live),
                    generation=w.generation,
                    observed=w.generation,
                )
            )
        return result

    def pods(self, namespace: str, selector: Mapping[str, str] | None = None) -> list[Pod]:
        self._call("pods")
        ns = self._live(namespace)
        return [
            self._pod_view(p)
            for p in sorted(ns.pods.values(), key=lambda p: p.name)
            if all(p.labels.get(k) == v for k, v in (selector or {}).items())
        ]

    def delete_pods(self, namespace: str, names: Sequence[str], *, grace_secs: int) -> None:
        self._call("delete_pods")
        ns = self._live(namespace)
        for name in names:
            pod = ns.pods.get(name)
            if pod is not None and pod.deleted_at is None:
                pod.deleted_at, pod.grace = self.clock.now(), float(grace_secs)

    def scale(self, namespace: str, deployment: str, replicas: int) -> None:
        self._call("scale")
        ns = self._writable(namespace)
        if deployment not in ns.workloads:
            raise NotFoundError(
                f'Error from server (NotFound): deployments.apps "{deployment}" not found'
            )
        ns.workloads[deployment].replicas = replicas
        ns.workloads[deployment].generation += 1
        self._reconcile(ns)

    def exec(self, namespace: str, pod: str, argv: Sequence[str], *, timeout: float) -> ExecResult:
        del timeout  # a fake command answers at once
        self._call("exec")
        target = self._pod(namespace, pod)
        self._finish_due(target)
        if len(argv) == len(("cat", "FILE")) and argv[0] == "cat":
            data = target.files.get(argv[1])
            if data is None:
                return ExecResult(
                    exit_code=1, output=f"cat: {argv[1]}: No such file or directory\n"
                )
            return ExecResult(exit_code=0, output=data.decode())
        if list(argv) == ["redis-cli", "FLUSHALL"] or (
            list(argv[:2]) == ["bash", "-c"] and "flush_all" in argv[2]
        ):
            self._live(namespace).flushed.append(pod)
            return ExecResult(exit_code=0, output="OK\n")
        return ExecResult(exit_code=127, output=f"{argv[0]}: not found\n")

    def _finish_due(self, pod: _Pod) -> None:
        """Publish a finished command's exit marker and outputs once its time has come."""
        outcome = pod.outcome
        if (
            outcome is None
            or pod.done_at is None
            or self.clock.now() < pod.done_at
            or outcome.crash
        ):
            return
        for name, data in outcome.outputs.items():
            pod.files.setdefault(f"{_pods.WORK_DIR}/{name}", data)
        pod.files.setdefault(
            f"{_pods.CONTROL}/{_pods.EXIT_MARK}", f"{outcome.exit_code}\n".encode()
        )

    def put_files(
        self, namespace: str, pod: str, directory: str, files: Mapping[str, bytes]
    ) -> None:
        self._call("put_files")
        target = self._pod(namespace, pod)
        for name, data in files.items():
            target.files[f"{directory}/{name}"] = data
        ready = f"{_pods.CONTROL}/{_pods.READY_MARK}"
        if ready in target.files and target.outcome is None and target.argv:
            prefix = f"{_pods.WORK_DIR}/"
            inputs = {k[len(prefix) :]: v for k, v in target.files.items() if k.startswith(prefix)}
            program = self.programs.get(target.argv[0])
            outcome = (
                program(target.argv, inputs)
                if program is not None
                else Outcome(exit_code=127, log=f"sh: {target.argv[0]}: not found\n", secs=0.1)
            )
            target.outcome, target.done_at, target.log = (
                outcome,
                self.clock.now() + outcome.secs,
                outcome.log,
            )
        if f"{_pods.CONTROL}/{_pods.COLLECTED_MARK}" in target.files and target.outcome is not None:
            self._finish_due(target)
            if f"{_pods.CONTROL}/{_pods.EXIT_MARK}" in target.files:
                target.finished = True

    def get_files(self, namespace: str, pod: str, directory: str, into: Path) -> list[str]:
        self._call("get_files")
        target = self._pod(namespace, pod)
        self._finish_due(target)
        names = []
        for key, data in sorted(target.files.items()):
            if key.startswith(f"{directory}/"):
                relative = key[len(directory) + 1 :]
                path = into / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                names.append(relative)
        into.mkdir(parents=True, exist_ok=True)
        return names

    def logs(
        self, namespace: str, pod: str, *, tail: int | None = None, previous: bool = False
    ) -> str:
        self._call("logs")
        target = self._live(namespace).pods.get(pod)
        if target is None or previous:
            return ""
        lines = target.log.splitlines(keepends=True)
        return "".join(lines[-tail:] if tail else lines)

    def pod_stats(self, namespace: str, node: str) -> dict[str, PodStats]:
        self._call("pod_stats")
        now = self.clock.now()
        return {
            p.name: PodStats(cpu_seconds=max(now - p.created - p.start_secs, 0.0) * 0.5)
            for p in self._live(namespace).pods.values()
            if p.node == node and self._running(p)
        }


class FakeBuilder:
    """Records builds (the image names and contexts) and fails those in `fail`."""

    def __init__(self) -> None:
        self.builds: list[_build.Build] = []
        self.fail: set[str] = set()

    def build(
        self, builds: Sequence[_build.Build], *, timeout_secs: float, log_dir: Path
    ) -> dict[str, float]:
        del timeout_secs, log_dir  # a fake build takes no time and writes no log
        for build in builds:
            if not (build.context / "Dockerfile").is_file():
                raise _build.BuildError(f"build of {build.image}: no Dockerfile in {build.context}")
            if any(build.image.startswith(prefix) for prefix in self.fail):
                raise _build.BuildError(
                    f"build of {build.image} failed (buildctl exit 1); log tail:\nfake"
                )
            self.builds.append(build)
        return {build.image: 1.0 for build in builds}
