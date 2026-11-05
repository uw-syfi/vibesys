"""A small task on the fake cluster: a data tier (db), two app Deployments, a cache, a gateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from vibesys_evaluator.deploy import k8s
from vibesys_evaluator.deploy._k8s import (
    AppSource,
    Cluster,
    Environment,
    FakeBuilder,
    FakeClock,
    FakeKube,
    Outcome,
    PodCommand,
)

if TYPE_CHECKING:
    from pathlib import Path

MANIFEST = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: config
data:
  db: db.${NAMESPACE}.svc.cluster.local:27017
---
apiVersion: v1
kind: Service
metadata: {name: db}
spec: {selector: {app: db}, ports: [{port: 27017}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: db, labels: {tier: data}}
spec:
  replicas: 1
  selector: {matchLabels: {app: db}}
  template:
    metadata: {labels: {app: db}}
    spec: {containers: [{name: db, image: mongo:4.4}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: cache, labels: {tier: app}}
spec:
  replicas: 1
  selector: {matchLabels: {app: cache}}
  template:
    metadata: {labels: {app: cache}}
    spec: {containers: [{name: cache, image: memcached:1.6}]}
---
apiVersion: v1
kind: Service
metadata: {name: frontend, namespace: "${NAMESPACE}"}
spec: {selector: {app: frontend}, ports: [{port: 5000}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: frontend, labels: {tier: app}}
spec:
  replicas: 2
  selector: {matchLabels: {app: frontend}}
  template:
    metadata: {labels: {app: frontend}}
    spec: {containers: [{name: frontend, image: "${APP_IMAGE}"}]}
---
apiVersion: apps/v1
kind: Deployment
metadata: {name: backend, labels: {tier: app}}
spec:
  replicas: 2
  selector: {matchLabels: {app: backend}}
  template:
    metadata: {labels: {app: backend}}
    spec: {containers: [{name: backend, image: "${APP_IMAGE}"}]}
"""

SEED = PodCommand(
    argv=("python", "/task/bench.py", "pod", "seed"), inputs={"dataset.pickle": b"data"}
)
PROBE_PLAN = b"probe-plan"


def make_app(**overrides: object) -> k8s.App[Any]:
    fields: dict[str, Any] = {
        "name": "shop",
        "manifest": "k8s/manifest.yaml",
        "images": {"APP_IMAGE": "app"},
        "gateway": "http://frontend:5000",
        "ready": ["GET /"],  # stands in for service.get("/"): the deploy layer only counts them
        "data_tier": "tier=data",
        "seed": lambda _data: None,
        "restartable": ["frontend", "backend"],
        "caches": {"cache": "memcached"},
    }
    fields.update(overrides)
    return k8s.App(**fields)


@dataclass
class World:
    """The fake cluster, builder and clock, a task directory and a project root."""

    kube: FakeKube
    builder: FakeBuilder
    clock: FakeClock
    env: Environment
    task_dir: Path
    project_root: Path
    lines: list[str]

    def log(self, line: str) -> None:
        self.lines.append(line)

    @property
    def cluster(self) -> Cluster:
        return Cluster(
            env=self.env,
            kube=self.kube,
            builder=self.builder,
            clock=self.clock,
            log=self.log,
            token=lambda: "t0",
        )

    def source(self, app: k8s.App[Any]) -> AppSource:
        return AppSource(
            app=app, task_dir=self.task_dir, project_root=self.project_root, probe_plan=PROBE_PLAN
        )


def make_world(root: Path, manifest: str = MANIFEST) -> World:
    clock = FakeClock()
    kube = FakeKube(clock)
    task_dir = root / "task"
    (task_dir / "k8s").mkdir(parents=True)
    (task_dir / "k8s" / "manifest.yaml").write_text(manifest)
    (task_dir / "Dockerfile").write_text("FROM vibesys-evaluator\nCOPY . /task\n")
    (task_dir / "vibesys.input.toml").write_text(
        "[accuracy]\ntimeout_seconds = 1800\n[benchmark]\ntimeout_seconds = 3600\n"
    )
    project_root = root / "project"
    (project_root / "app").mkdir(parents=True)
    (project_root / "app" / "Dockerfile").write_text("FROM scratch\n")
    artifacts = root / "artifacts" / "job-7" / "benchmark-0"
    artifacts.mkdir(parents=True)
    env = Environment.parse(
        {
            "KUBECONFIG": "/vibesys/kubeconfig",
            "VIBESYS_K8S_REGISTRY": "registry:5000",
            "VIBESYS_K8S_BUILDER": "tcp://builder:1234",
            "VIBESYS_ARTIFACTS_DIR": str(artifacts),
        },
        work_dir=root / "work",
        artifacts=None,
    )
    kube.programs["loadgen"] = lambda *_: Outcome(log="probe ok\n")
    kube.programs["python"] = lambda *_: Outcome(log="seeded\n", secs=20)
    return World(
        kube=kube,
        builder=FakeBuilder(),
        clock=clock,
        env=env,
        task_dir=task_dir,
        project_root=project_root,
        lines=[],
    )
