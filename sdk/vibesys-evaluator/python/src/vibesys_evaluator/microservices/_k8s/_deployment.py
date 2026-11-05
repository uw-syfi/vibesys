"""The deploy shell: bring an App up in a fresh namespace, run the package's pods, hooks, teardown.

Every decision is in the pure modules (`_manifest`, `_status`, `_pods`); this module only
sequences `KubeAPI` and `Builder` calls, waits with the injected `Clock`, and writes
artifacts. The namespace is owned by one `Deployment` and deleted by `close()`, which
every exit path of the orchestrator passes through (return, exception, signal).
"""

from __future__ import annotations

import json
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from vibesys_evaluator.microservices._k8s import _build, _manifest, _pods, _status, _validate
from vibesys_evaluator.microservices._k8s._api import (
    ConflictError,
    KubeAPI,
    KubeError,
    NotFoundError,
    Pod,
)
from vibesys_evaluator.microservices._k8s._pods import Role
from vibesys_evaluator.microservices._private.deploy import DeployError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from types import TracebackType

    from vibesys_evaluator.microservices._k8s._clock import Clock
    from vibesys_evaluator.microservices.k8s import App

WAIT_SECS = 300.0  # data tier, all replicas, a pod's start, a restart
SEED_SECS = 1200.0
PROBE_SECS = 300.0
BUILD_SECS = 2400.0
DELETE_WAIT_SECS = 240.0
DELETE_ATTEMPTS = 3
POLL_SECS = 2.0
EXIT_POLL_SECS = 1.0
LOG_TAIL = 1500
#: The termination reason of a container the kernel killed at its memory limit.
OOM_KILLED = "OOMKilled"


class PodOutOfMemoryError(DeployError):
    """A pod of the package was OOMKilled; ``limit`` is its memory limit (``4Gi``)."""

    def __init__(self, message: str, *, limit: str) -> None:
        super().__init__(message)
        self.limit = limit


PROBE_FILE = "probe.binpb"

MEMCACHED_FLUSH = (
    "exec 3<>/dev/tcp/127.0.0.1/11211; printf 'flush_all\\r\\n' >&3; "
    'read -r reply <&3; echo "$reply"'
)


def _stopping(error: BaseException | None) -> bool:
    """A signal (the orchestrator's Stopped), Ctrl-C or exit, not a failure: tear down fast."""
    return error is not None and not isinstance(error, Exception)


@dataclass(frozen=True)
class Environment:
    """What vibesys-cli's `kubernetes` resource and the job give the orchestrator."""

    registry: str
    builder: str
    artifacts: Path | None
    work_dir: Path
    job: str

    @staticmethod
    def parse(environ: Mapping[str, str], *, work_dir: Path, artifacts: Path | None) -> Environment:
        """Read the job's variables; artifacts overrides $VIBESYS_ARTIFACTS_DIR when given."""
        for name in ("KUBECONFIG", "VIBESYS_K8S_REGISTRY", "VIBESYS_K8S_BUILDER"):
            if not environ.get(name):
                raise DeployError(
                    f"{name} is not set: the task needs `[environment] needs = "
                    '["kubernetes"]` and a placement whose kubernetes resource names '
                    "the registry and the builder"
                )
        job_dir = environ.get("VIBESYS_ARTIFACTS_DIR", "")
        if artifacts is None and job_dir:
            artifacts = Path(job_dir)
        return Environment(
            registry=environ["VIBESYS_K8S_REGISTRY"].rstrip("/"),
            builder=environ["VIBESYS_K8S_BUILDER"],
            artifacts=artifacts,
            work_dir=work_dir,
            job=_status.job_label(job_dir, secrets.token_hex(4)),
        )


@dataclass(frozen=True)
class PodCommand:
    """A command of the task image, and the files written into its pod's WORK_DIR first."""

    argv: tuple[str, ...]
    inputs: Mapping[str, bytes] = field(default_factory=dict)


@dataclass(frozen=True)
class PodRun:
    """A finished pod: its command's exit status, its outputs and its log.

    `outputs` holds the files the command left in WORK_DIR, its inputs excepted.
    """

    name: str
    exit_code: int
    outputs: Path
    files: tuple[str, ...]
    log: str
    seconds: float


@dataclass(frozen=True)
class Cluster:
    """Where and with what the deploy layer works: the job's environment and the interfaces."""

    env: Environment
    kube: KubeAPI
    builder: _build.Builder
    clock: Clock
    log: Callable[[str], None]
    token: Callable[[], str] = field(default=lambda: secrets.token_hex(4))  # namespace suffixes


@dataclass(frozen=True)
class AppSource:
    """An App with what it is built from.

    The task directory (manifest, task image), the project root (candidate images) and the
    encoded `ready` requests (a loadgen requests file).
    """

    app: App[Any]
    task_dir: Path
    project_root: Path
    probe_plan: bytes


@dataclass(frozen=True)
class _Target:
    """What one Deployment acts on."""

    source: AppSource
    namespace: str
    task_image: str
    rendered: _manifest.Rendered


def task_timeout_secs(task_dir: Path) -> int:
    """The longest a job of the task may run: the larger of its two timeout_seconds."""
    path = task_dir / "vibesys.input.toml"
    try:
        manifest = tomllib.loads(path.read_text())
        return max(
            int(manifest[section]["timeout_seconds"])
            for section in ("accuracy", "benchmark")
            if section in manifest
        )
    except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError, TypeError) as error:
        raise DeployError(
            f"{path}: cannot read [accuracy]/[benchmark] timeout_seconds ({error})"
        ) from None


def sweep(source: AppSource, *, cluster: Cluster) -> list[str]:
    """Delete this task's namespaces that killed samples left behind; return their names.

    Only namespaces labelled with the App's name, named with its prefix and older than the
    longest a job may run plus a margin are deleted, each only if it still has the UID it
    was listed with, so a concurrent sample's namespace or another task's is never touched.
    """
    app, kube, log = source.app, cluster.kube, cluster.log
    ttl = task_timeout_secs(source.task_dir) + _status.SWEEP_MARGIN_SECS
    listed = kube.namespaces({_status.TASK_LABEL: app.name})
    swept: list[str] = []
    for namespace in _status.stale(listed, app_name=app.name, now=cluster.clock.now(), ttl=ttl):
        try:
            kube.delete_namespace(namespace.name, uid=namespace.uid)
        except KubeError as error:  # gone, recreated, or refused: never retried against a new UID
            log(f"sweep: left {namespace.name} ({type(error).__name__}: {str(error)[:200]})")
            continue
        log(
            f"sweep: deleted stale namespace {namespace.name} (job "
            f"{namespace.labels.get(_status.JOB_LABEL, '?')})"
        )
        swept.append(namespace.name)
    return swept


def _images(
    source: AppSource, *, registry: str, staging: Path
) -> tuple[dict[str, str], str, list[_build.Build]]:
    """The candidate images by manifest variable, the task image, and the builds that make them."""
    app, project_root, task_dir = source.app, source.project_root, source.task_dir
    builds: dict[str, _build.Build] = {}
    images: dict[str, str] = {}
    app_slug = _status.slug(app.name)
    for variable, directory in sorted(app.images.items()):
        context = project_root / directory
        if not (context / "Dockerfile").is_file():
            raise DeployError(f"k8s.App(images[{variable!r}]): {context}/Dockerfile not found")
        image = _build.image_name(
            registry, app_slug, Path(directory).name, _build.tree_digest(context)
        )
        images[variable] = image
        builds[image] = _build.Build(context=context, image=image)
    if not (task_dir / "Dockerfile").is_file():
        raise DeployError(f"{task_dir}/Dockerfile not found: the phase pods run the task image")
    task_image = _build.image_name(registry, app_slug, "task", _build.tree_digest(task_dir))
    builds[task_image] = _build.Build(context=_build.stage(task_dir, staging), image=task_image)
    return images, task_image, list(builds.values())


class Deployment:
    """An App in its namespace. Use `deploy()` (owns the namespace) or `attach()` (does not)."""

    def __init__(self, target: _Target, cluster: Cluster, *, owned: bool) -> None:
        self.app = target.source.app
        self.namespace = target.namespace
        self.task_image = target.task_image
        self.gateway = _validate.parse_gateway(self.app.gateway)
        self._rendered = target.rendered
        self._probe_plan = target.source.probe_plan
        self._kube = cluster.kube
        self._clock = cluster.clock
        self._log = cluster.log
        self._artifacts = cluster.env.artifacts
        self._work_dir = cluster.env.work_dir
        self._uid: str | None = None
        self._creating = False  # set before the create request: a cut-short one may have succeeded
        self._owned = owned
        self._closed = False
        self._pods = 0
        self._usage: dict[str, _status.Usage] = {}

    # -- lifecycle ------------------------------------------------------------------------

    def __enter__(self) -> Deployment:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        _tb: TracebackType | None,
    ) -> None:
        self.close(fast=_stopping(error), failed=error is not None)

    def create_namespace(self, labels: Mapping[str, str]) -> None:
        """Create the namespace this Deployment owns (deploy() does, once)."""
        self._creating = True
        self._uid = self._kube.create_namespace(self.namespace, labels).uid

    def close(self, *, fast: bool = False, failed: bool = False) -> None:
        """Delete the namespace (if this Deployment owns it); idempotent.

        fast (after a signal): ask for the deletion and return, within the CLI's grace
        period. Otherwise save diagnostics when failed, then wait (bounded) until the
        namespace is gone so the next sample finds the cluster free. A signal during close
        ends the wait, never the deletion request, and is raised again once it is sent.
        """
        if self._closed or not self._owned or not self._creating:
            return
        self._closed = True
        if failed and not fast:
            self._save_diagnostics()
        stop: BaseException | None = None
        try:
            uid = self._uid or self._find_uid()
        except BaseException as error:
            if not _stopping(error):
                raise
            stop, uid = error, self._find_uid()
        if uid is not None:
            try:
                self._request_delete(uid)
            except BaseException as error:
                if not _stopping(error):
                    raise
                stop = stop or error
                self._request_delete(uid)  # later signals are ignored: this request completes
            if not fast and stop is None:
                self._wait_deleted()
        if stop is not None:
            raise stop

    def _find_uid(self) -> str | None:
        """The namespace's UID when its creation was cut short before its answer arrived."""
        for namespace in self._kube.namespaces({_status.TASK_LABEL: self.app.name}):
            if namespace.name == self.namespace:
                return namespace.uid
        return None

    def _request_delete(self, uid: str) -> None:
        """Ask for the deletion, retrying a failed request twice; gone or recreated is final."""
        self._log(f"deleting namespace {self.namespace}")
        for attempt in range(DELETE_ATTEMPTS):
            try:
                self._kube.delete_namespace(self.namespace, uid=uid)
            except (NotFoundError, ConflictError) as error:
                self._log(f"namespace {self.namespace}: {str(error)[:300]}")
                return
            except KubeError as error:
                self._log(
                    f"namespace {self.namespace}: deletion attempt {attempt + 1} failed: {str(error)[:300]}"
                )
            else:
                return
        self._log(f"namespace {self.namespace} left for the next sample's sweep")

    def _wait_deleted(self) -> None:
        deadline = self._clock.monotonic() + DELETE_WAIT_SECS
        while self._clock.monotonic() < deadline:
            names = {ns.name for ns in self._kube.namespaces({_status.TASK_LABEL: self.app.name})}
            if self.namespace not in names:
                return
            self._clock.sleep(POLL_SECS)
        self._log(f"namespace {self.namespace}: still terminating after {DELETE_WAIT_SECS:.0f}s")

    # -- bring-up ---------------------------------------------------------------------------

    def bring_up(self, seed: PodCommand | None) -> None:
        """Apply, wait, seed and probe in the order the data tier asks for (deploy() does, once)."""
        tier = self._rendered.rest != ()
        self._log(f"applying {'the data tier' if tier else 'the manifest'} into {self.namespace}")
        self._kube.apply(self.namespace, self._rendered.first)
        self.wait_serving(names=self._workloads(self._rendered.first))
        if tier:
            self._seed(seed)
            self._log("applying the application")
            self._kube.apply(self.namespace, self._rendered.rest)
            self.wait_serving()
            self.probe()
        else:
            self.wait_serving()
            self.probe()
            self._seed(seed)

    def _seed(self, seed: PodCommand | None) -> None:
        if seed is None:
            return
        self._log("seeding: starting the seed pod")
        run = self.run_pod(Role.SEED, seed, timeout_secs=SEED_SECS)
        if run.exit_code != 0:
            raise DeployError(
                f"seed failed (exit {run.exit_code}) after {run.seconds:.0f}s; "
                f"log tail:\n{run.log[-LOG_TAIL:]}"
            )
        self._log(f"seeded in {run.seconds:.0f}s")

    @staticmethod
    def _workloads(objects: Sequence[Mapping[str, Any]]) -> set[str]:
        return _manifest.workload_names(objects) | _manifest.workload_names(objects, "StatefulSet")

    def wait_serving(
        self,
        *,
        names: set[str] | None = None,
        replaced: frozenset[str] = frozenset(),
        timeout_secs: float = WAIT_SECS,
    ) -> None:
        """Wait until every workload (of names) has all its replicas Ready and available."""
        deadline = self._clock.monotonic() + timeout_secs
        while True:
            pods = self._kube.pods(self.namespace)
            app_pods = [p for p in pods if _pods.POD_LABEL not in p.labels]
            reasons = _status.unready(
                self._kube.workloads(self.namespace), app_pods, names=names, replaced=replaced
            )
            if not reasons:
                return
            fatal = _status.fatal(app_pods)
            if fatal or self._clock.monotonic() >= deadline:
                why = "cannot start" if fatal else f"not serving after {timeout_secs:.0f}s"
                raise DeployError(f"{why}: " + "; ".join((fatal or reasons)[:12]))
            self._clock.sleep(POLL_SECS)

    def probe(self) -> None:
        """Loadgen probe from a pod: every ready request succeeds twice per ready gateway pod."""
        service = next(
            (
                obj
                for obj in self._rendered.objects
                if obj["kind"] == "Service" and obj["metadata"]["name"] == self.gateway.service
            ),
            None,
        )
        if service is None:
            raise DeployError(
                f"k8s.App(gateway={self.app.gateway!r}): the manifest has no Service "
                f"{self.gateway.service!r}"
            )
        selector = dict(service.get("spec", {}).get("selector") or {})
        ready = [p for p in self._kube.pods(self.namespace, selector) if p.ready and not p.deleting]
        rounds = 2 * max(len(ready), 1)
        argv = (
            "loadgen",
            "probe",
            "--target",
            self.gateway.url,
            "--requests",
            f"{_pods.WORK_DIR}/{PROBE_FILE}",
            "--rounds",
            str(rounds),
            "--deadline",
            str(int(PROBE_SECS)),
        )
        run = self.run_pod(
            Role.PROBE,
            PodCommand(argv=argv, inputs={PROBE_FILE: self._probe_plan}),
            timeout_secs=PROBE_SECS + WAIT_SECS,
        )
        if run.exit_code != 0:
            raise DeployError(
                f"not ready: the probes through {self.gateway.url} failed "
                f"(loadgen probe exit {run.exit_code}); log tail:\n{run.log[-LOG_TAIL:]}"
            )
        self._log(f"ready: probes answered through {self.gateway.url} ({rounds} rounds)")

    # -- the package's pods ----------------------------------------------------------------------

    def run_pod(
        self, role: Role, command: PodCommand, *, timeout_secs: float, outputs: Path | None = None
    ) -> PodRun:
        """Run a command of the task image in a pod of the namespace and collect WORK_DIR.

        The pod gets `command.inputs` in WORK_DIR before the command starts; the files the
        command leaves there are copied into `outputs` (a fresh directory by default). A
        nonzero exit is returned, not raised (the caller judges it); a pod that cannot
        start, dies or runs past timeout_secs raises DeployError with its log tail.
        """
        self._pods += 1
        name = f"vse-{role}-{self._pods}"
        started = self._clock.monotonic()
        deadline = int(timeout_secs + 2 * WAIT_SECS)
        self._kube.apply(
            self.namespace,
            [
                _pods.job(
                    name=name,
                    role=role,
                    image=self.task_image,
                    argv=command.argv,
                    deadline_secs=deadline,
                )
            ],
        )
        pod = self._wait_pod_running(name)
        work = _pods.WORK_DIR.removeprefix(f"{_pods.ROOT}/")
        files = {f"{work}/{key}": value for key, value in command.inputs.items()}
        files[f"control/{_pods.READY_MARK}"] = b""
        self._kube.put_files(self.namespace, pod, _pods.ROOT, files)
        code = self._wait_exit(name, pod, started + timeout_secs)
        outputs = outputs or self._work_dir / name
        names = [
            n
            for n in self._kube.get_files(self.namespace, pod, _pods.WORK_DIR, outputs)
            if n not in command.inputs
        ]
        for key in command.inputs:
            (outputs / key).unlink(missing_ok=True)
        self._kube.put_files(
            self.namespace, pod, _pods.ROOT, {f"control/{_pods.COLLECTED_MARK}": b""}
        )
        log = self._kube.logs(self.namespace, pod)
        self.write_artifact(f"{name}.log", log)
        return PodRun(
            name=name,
            exit_code=code,
            outputs=outputs,
            files=tuple(names),
            log=log,
            seconds=round(self._clock.monotonic() - started, 1),
        )

    def _job_pod(self, name: str) -> Pod | None:
        pods = self._kube.pods(self.namespace, {"job-name": name})
        return pods[0] if pods else None

    def _pod_failure(self, name: str, pod: Pod) -> DeployError:
        log = self._kube.logs(self.namespace, pod.name)
        self.write_artifact(f"{name}.log", log)
        state = pod.waiting or pod.phase
        code = f", exit {pod.exit_code}" if pod.exit_code is not None else ""
        if pod.reason == OOM_KILLED:
            limit = _pods.SIZES[Role(pod.labels.get(_pods.POD_LABEL, ""))].memory
            return PodOutOfMemoryError(
                f"pod {name} ({pod.name}) was killed for running out of memory "
                f"(OOMKilled at its {limit} limit{code}); log tail:\n{log[-LOG_TAIL:]}",
                limit=limit,
            )
        return DeployError(f"pod {name} ({pod.name}) {state}{code}; log tail:\n{log[-LOG_TAIL:]}")

    def _wait_pod_running(self, name: str) -> str:
        deadline = self._clock.monotonic() + WAIT_SECS
        while True:
            pod = self._job_pod(name)
            if pod is not None and pod.phase == "Running":
                return pod.name
            if pod is not None and (
                pod.phase in ("Succeeded", "Failed") or pod.waiting in _status.FATAL_WAITING
            ):
                raise self._pod_failure(name, pod)
            if self._clock.monotonic() >= deadline:
                state = "no pod" if pod is None else (pod.waiting or pod.phase)
                raise DeployError(f"pod {name} did not start in {WAIT_SECS:.0f}s ({state})")
            self._clock.sleep(POLL_SECS)

    def _wait_exit(self, name: str, pod: str, deadline: float) -> int:
        mark = f"{_pods.CONTROL}/{_pods.EXIT_MARK}"
        while True:
            try:
                result = self._kube.exec(self.namespace, pod, ["cat", mark], timeout=30)
            except (
                KubeError
            ):  # the container stopped (or the API blinked): the pod's state says which
                result = None
            if (
                result is not None
                and result.exit_code == 0
                and result.output.strip().lstrip("-").isdigit()
            ):
                return int(result.output.strip())
            current = self._job_pod(name)
            if current is None or current.phase != "Running":
                raise (
                    self._pod_failure(name, current)
                    if current
                    else DeployError(f"pod {name} vanished")
                )
            if self._clock.monotonic() >= deadline:
                raise DeployError(
                    f"pod {name} did not finish in time; log tail:\n"
                    f"{self._kube.logs(self.namespace, pod, tail=40)[-LOG_TAIL:]}"
                )
            self._clock.sleep(EXIT_POLL_SECS)

    # -- hooks for accuracy checkers ---------------------------------------------------------------

    def _workload_pods(self, name: str) -> list[Pod]:
        workload = next((w for w in self._kube.workloads(self.namespace) if w.name == name), None)
        if workload is None:
            raise DeployError(f"no Deployment {name!r} in {self.namespace}")
        return self._kube.pods(self.namespace, workload.selector)

    def restart(self, deployments: Sequence[str], *, every_pod: bool = False) -> None:
        """Delete one pod (the first by name) or every pod of each Deployment at once; wait.

        Returns when no deleted pod remains, every workload serves with all replicas, and
        the probes pass.
        """
        allowed = set(self.app.restartable) | set(self.app.caches)
        for name in deployments:
            if name not in allowed:
                raise DeployError(
                    f"restart {name}: not in k8s.App(restartable) or caches "
                    f"({', '.join(sorted(allowed)) or 'none'})"
                )
        replaced: set[str] = set()
        for name in deployments:
            chosen = _status.victims(self._workload_pods(name), every=every_pod)
            self._kube.delete_pods(self.namespace, [p.name for p in chosen], grace_secs=1)
            replaced.update(p.uid for p in chosen)
            self._log(f"restart: deleted {', '.join(p.name for p in chosen)}")
        self.wait_serving(replaced=frozenset(replaced))
        self.probe()

    def flush(self, cache: str) -> None:
        """Empty a cache in place, on every pod of its Deployment, then probe."""
        kind: Literal["memcached", "redis"] | None = self.app.caches.get(cache)
        if kind is None:
            raise DeployError(
                f"flush {cache}: not in k8s.App(caches) ({', '.join(sorted(self.app.caches))})"
            )
        argv = ["redis-cli", "FLUSHALL"] if kind == "redis" else ["bash", "-c", MEMCACHED_FLUSH]
        for pod in self._workload_pods(cache):
            if pod.deleting:
                continue
            result = self._kube.exec(self.namespace, pod.name, argv, timeout=60)
            if result.exit_code != 0 or "OK" not in result.output:
                raise DeployError(f"flush {cache} ({pod.name}): {result.output.strip()[-300:]}")
        self._log(f"flushed {cache}")
        self.probe()

    def stop(self) -> None:
        """Scale every restartable Deployment to 0 and wait until their pods are gone."""
        for name in self.app.restartable:
            self._kube.scale(self.namespace, name, 0)
        self.wait_serving(names=set(self.app.restartable))
        self._log(f"stopped {', '.join(self.app.restartable)}")

    def start(self) -> None:
        """Scale the restartable Deployments back to the manifest's replicas; wait; probe."""
        for obj in self._rendered.objects:
            if obj["kind"] == "Deployment" and obj["metadata"]["name"] in self.app.restartable:
                self._kube.scale(
                    self.namespace, obj["metadata"]["name"], int(obj["spec"].get("replicas", 1))
                )
        self.wait_serving()
        self.probe()

    # -- diagnostics -----------------------------------------------------------------------------

    def usage(self, label: str) -> dict[str, float]:
        """Per-pod CPU seconds since the previous snapshot, logged and in pod-usage-<label>.json.

        Diagnostics only: a cluster that does not give kubelet stats logs why and returns {}.
        """
        try:
            pods = [p for p in self._kube.pods(self.namespace) if _pods.POD_LABEL not in p.labels]
            stats = {}
            for node in sorted({p.node for p in pods if p.node}):
                stats.update(self._kube.pod_stats(self.namespace, node))
            now = _status.usage(pods, self._kube.workloads(self.namespace), stats)
        except KubeError as error:
            self._log(f"pod usage {label}: unavailable ({str(error)[:200]})")
            return {}
        delta = _status.usage_delta(self._usage, now)
        self._usage = now
        lines = [f"  {name} ({now[name].workload}): {cpu:.1f} CPU s" for name, cpu in delta.items()]
        self._log(f"pod usage {label}:\n" + "\n".join(lines))
        self.write_artifact(
            f"pod-usage-{label}.json",
            json.dumps(
                {
                    name: {
                        "workload": item.workload,
                        "cpu_seconds": item.cpu_seconds,
                        "cpu_seconds_delta": delta[name],
                        "rx_bytes": item.rx_bytes,
                        "tx_bytes": item.tx_bytes,
                    }
                    for name, item in now.items()
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return delta

    def _save_diagnostics(self) -> None:
        """The pods, and the log tails of those not serving or restarted, into the artifacts."""
        if self._artifacts is None:
            return
        try:
            pods = self._kube.pods(self.namespace)
            self.write_artifact(
                "pods.json",
                json.dumps(
                    [
                        {
                            "name": p.name,
                            "phase": p.phase,
                            "ready": p.ready,
                            "restarts": p.restarts,
                            "waiting": p.waiting,
                            "node": p.node,
                        }
                        for p in pods
                    ],
                    indent=2,
                )
                + "\n",
            )
            for pod in pods:
                if not pod.ready or pod.restarts:
                    text = self._kube.logs(self.namespace, pod.name, tail=300)
                    if pod.restarts:
                        text += "\n--- previous container ---\n" + self._kube.logs(
                            self.namespace, pod.name, tail=300, previous=True
                        )
                    self.write_artifact(f"logs/{pod.name}.log", text)
        except KubeError as error:
            self._log(f"diagnostics unavailable: {str(error)[:200]}")

    def write_artifact(self, name: str, text: str) -> None:
        """Write a file into $VIBESYS_ARTIFACTS_DIR, when the job has one."""
        if self._artifacts is not None:
            path = self._artifacts / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)


def _check_names(app: App[Any], rendered: _manifest.Rendered) -> None:
    """The App's Deployment and Service names exist in the manifest (before any build)."""
    deployments = _manifest.workload_names(rendered.objects)
    for field_name, names in (("restartable", app.restartable), ("caches", list(app.caches))):
        missing = sorted(set(names) - deployments)
        if missing:
            raise DeployError(
                f"k8s.App({field_name}): {', '.join(missing)} not a Deployment of {app.manifest}"
            )
    service = _validate.parse_gateway(app.gateway).service
    if service not in _manifest.service_names(rendered.objects):
        raise DeployError(
            f"k8s.App(gateway={app.gateway!r}): no Service {service!r} in {app.manifest}"
        )


def deploy(source: AppSource, *, seed: PodCommand | None, cluster: Cluster) -> Deployment:
    """Sweep, build, deploy into a fresh namespace, seed and probe; return the serving Deployment.

    The caller owns the result and must close it (use it as a context manager). On any
    failure here, including a signal, the namespace is deleted before the error propagates.
    """
    app, env = source.app, cluster.env
    if (seed is None) != (app.seed is None):
        raise DeployError(
            "deploy: a seed pod command is given exactly when k8s.App(seed=...) is set"
        )
    namespace = _status.namespace_name(app.name, cluster.token())
    text = _read_manifest(source)
    sweep(source, cluster=cluster)
    images, task_image, builds = _images(
        source, registry=env.registry, staging=env.work_dir / "task-context"
    )
    rendered = _manifest.render(
        text,
        namespace=namespace,
        images=images,
        data_tier=_validate.parse_selector(app.data_tier),
        source=app.manifest,
    )
    _check_names(app, rendered)
    target = _Target(source=source, namespace=namespace, task_image=task_image, rendered=rendered)
    deployment = Deployment(target, cluster, owned=True)
    deployment.write_artifact("manifest.rendered.yaml", rendered.text)
    try:
        cluster.log(f"building {len(builds)} images")
        seconds = cluster.builder.build(
            builds, timeout_secs=BUILD_SECS, log_dir=env.work_dir / "build"
        )
        summary = {"images": images, "task": task_image, "build_seconds": seconds}
        deployment.write_artifact(
            "images.json", json.dumps(summary, indent=2, sort_keys=True) + "\n"
        )
        deployment.create_namespace(
            _status.namespace_labels(app.name, now=cluster.clock.now(), job=env.job)
        )
        deployment.bring_up(seed)
    except _build.BuildError as error:
        deployment.close(failed=True)
        raise DeployError(str(error)) from None
    except BaseException as error:
        deployment.close(fast=_stopping(error), failed=True)
        raise
    return deployment


def _read_manifest(source: AppSource) -> str:
    try:
        return (source.task_dir / source.app.manifest).read_text()
    except OSError as error:
        raise DeployError(f"k8s.App(manifest={source.app.manifest!r}): {error}") from None


def attach(source: AppSource, *, namespace: str, cluster: Cluster) -> Deployment:
    """A Deployment for a namespace another process deployed (the `ctl` hooks); never deletes it."""
    app = source.app
    if not namespace.startswith(_status.namespace_prefix(app.name)):
        raise DeployError(f"ctl: namespace {namespace!r} is not one of {app.name}'s")
    if all(ns.name != namespace for ns in cluster.kube.namespaces({_status.TASK_LABEL: app.name})):
        raise DeployError(f"ctl: no namespace {namespace!r} labelled for {app.name}")
    # Only replica counts and selectors are read here: the image names do not matter.
    rendered = _manifest.render(
        _read_manifest(source),
        namespace=namespace,
        images={v: v.lower() for v in app.images},
        data_tier=_validate.parse_selector(app.data_tier),
        source=app.manifest,
    )
    digest = _build.tree_digest(source.task_dir)
    task_image = _build.image_name(cluster.env.registry, _status.slug(app.name), "task", digest)
    target = _Target(source=source, namespace=namespace, task_image=task_image, rendered=rendered)
    return Deployment(target, cluster, owned=False)


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
