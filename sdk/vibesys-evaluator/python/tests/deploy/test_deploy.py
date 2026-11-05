"""deploy(): ordering, images, readiness, phase pods, hooks and teardown, on the fake cluster."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from k8s_world import SEED, World, make_app, make_world

from vibesys_evaluator._private.cli import Stopped
from vibesys_evaluator.deploy._k8s import (
    WORK_DIR,
    DeployError,
    Deployment,
    FakeKube,
    KubeError,
    Outcome,
    PodCommand,
    Role,
    attach,
    ctl,
    deploy,
    tree_digest,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


def up(world: World, **overrides: object) -> Deployment:
    app = make_app(**overrides)
    seed = SEED if app.seed is not None else None
    return deploy(world.source(app), seed=seed, cluster=world.cluster)


def workload_names(kube: FakeKube, namespace: str) -> set[str]:
    return {name for (kind, name) in kube.namespace_objects(namespace) if kind == "Deployment"}


def test_the_seed_runs_after_the_data_tier_serves_and_before_the_application_exists(
    world: World,
) -> None:
    seen: dict[str, Any] = {}

    def seed(argv: Sequence[str], inputs: Mapping[str, bytes]) -> Outcome:
        seen["argv"], seen["inputs"] = tuple(argv), dict(inputs)
        seen["workloads"] = workload_names(world.kube, "hk8s-shop-t0")
        seen["db_ready"] = all(p.ready for p in world.kube.pods("hk8s-shop-t0", {"app": "db"}))
        return Outcome(secs=30)

    world.kube.programs["python"] = seed
    probes: list[set[str]] = []
    world.kube.programs["loadgen"] = lambda *_: (
        probes.append(workload_names(world.kube, "hk8s-shop-t0")) or Outcome()
    )
    with up(world) as deployment:
        assert deployment.namespace == "hk8s-shop-t0"
        assert seen["workloads"] == {"db"}
        assert seen["db_ready"]
        assert seen["argv"] == SEED.argv
        assert seen["inputs"] == SEED.inputs
        assert probes == [{"db", "cache", "frontend", "backend"}]
        assert all(p.ready for p in world.kube.pods("hk8s-shop-t0") if p.labels.get("app"))
    assert world.kube.all_namespaces() == []


def test_without_a_data_tier_the_seed_runs_once_the_whole_app_serves(world: World) -> None:
    order: list[str] = []
    world.kube.programs["python"] = lambda *_: order.append("seed") or Outcome()
    world.kube.programs["loadgen"] = lambda *_: order.append("probe") or Outcome()
    with up(world, data_tier=""):
        assert order == ["probe", "seed"]


def test_images_are_built_before_the_namespace_and_substituted_by_digest(world: World) -> None:
    with up(world) as deployment:
        digest = tree_digest(world.project_root / "app")
        built = [b.image for b in world.builder.builds]
        app_image = f"registry:5000/vse/shop/app:{digest}"
        assert app_image in built
        assert deployment.task_image in built
        assert deployment.task_image == f"registry:5000/vse/shop/task:{tree_digest(world.task_dir)}"
        frontend = world.kube.namespace_objects(deployment.namespace)[("Deployment", "frontend")]
        assert frontend["spec"]["template"]["spec"]["containers"][0]["image"] == app_image
        assert world.kube.calls.index("create_namespace") > 0
        jobs = [
            o
            for (kind, _), o in world.kube.namespace_objects(deployment.namespace).items()
            if kind == "Job"
        ]
        assert {job["spec"]["template"]["spec"]["containers"][0]["image"] for job in jobs} == {
            deployment.task_image
        }


def test_a_changed_candidate_gets_a_new_tag_and_generated_files_do_not(tmp_path: Path) -> None:
    root = tmp_path / "ctx"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n")
    before = tree_digest(root)
    (root / "pkg" / "__pycache__").mkdir()
    (root / "pkg" / "__pycache__" / "a.cpython-312.pyc").write_bytes(b"\0")
    assert tree_digest(root) == before
    (root / "pkg" / "a.py").write_text("x = 2\n")
    assert tree_digest(root) != before


def test_a_build_failure_creates_no_namespace(world: World) -> None:
    world.builder.fail.add("registry:5000/vse/shop/app:")
    with pytest.raises(DeployError, match="build of registry:5000/vse/shop/app"):
        up(world)
    assert "create_namespace" not in world.kube.calls
    assert world.kube.all_namespaces() == []


def test_manifest_errors_come_before_any_build(world: World) -> None:
    with pytest.raises(DeployError, match="restartable"):
        up(world, restartable=["frontend", "nosuch"])
    assert world.builder.builds == []


def test_an_app_that_never_serves_fails_with_the_reason_and_is_deleted(world: World) -> None:
    world.kube.broken["backend"] = "CrashLoopBackOff"
    with pytest.raises(
        DeployError, match=r"deployment/backend: 0/2 ready \(backend-\w+: CrashLoopBackOff"
    ):
        up(world)
    assert world.kube.all_namespaces() == []
    assert (world.env.artifacts / "pods.json").is_file()  # type: ignore[operator]


def test_a_failed_seed_fails_the_deploy_with_its_log(world: World) -> None:
    world.kube.programs["python"] = lambda *_: Outcome(exit_code=3, log="mongo refused\n")
    with pytest.raises(DeployError, match=r"(?s)seed failed \(exit 3\).*mongo refused"):
        up(world)
    assert world.kube.all_namespaces() == []


def test_a_phase_pod_gets_its_inputs_and_returns_its_outputs(world: World) -> None:
    def phase(_argv: Sequence[str], inputs: Mapping[str, bytes]) -> Outcome:
        return Outcome(
            exit_code=0,
            secs=95,
            log="phase done\n",
            outputs={
                "phase-output.binpb": inputs["phase-input.binpb"][::-1],
                "results.binpb.gz": b"r",
            },
        )

    world.kube.programs["vse-phase"] = phase
    with up(world) as deployment:
        run = deployment.run_pod(
            Role.PHASE,
            PodCommand(
                argv=("vse-phase", "--work", WORK_DIR),
                inputs={"phase-input.binpb": b"abc"},
            ),
            timeout_secs=600,
        )
        assert run.exit_code == 0
        assert run.files == ("phase-output.binpb", "results.binpb.gz")
        assert (run.outputs / "phase-output.binpb").read_bytes() == b"cba"
        assert run.seconds >= 95
        assert "phase done" in run.log
        job = world.kube.namespace_objects(deployment.namespace)[("Job", run.name)]
        resources = job["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert resources["limits"] == resources["requests"] == {"cpu": "6", "memory": "4Gi"}


def test_a_phase_pod_that_dies_raises_with_its_log(world: World) -> None:
    world.kube.programs["vse-phase"] = lambda *_: Outcome(crash=True, log="OOMKilled\n", secs=5)
    with (
        up(world) as deployment,
        pytest.raises(DeployError, match=r"(?s)Failed, exit 137.*OOMKilled"),
    ):
        deployment.run_pod(Role.PHASE, PodCommand(argv=("vse-phase",)), timeout_secs=600)


def test_a_phase_pod_nonzero_exit_is_returned(world: World) -> None:
    world.kube.programs["vse-phase"] = lambda *_: Outcome(exit_code=4)
    with up(world) as deployment:
        assert (
            deployment.run_pod(
                Role.PHASE, PodCommand(argv=("vse-phase",)), timeout_secs=60
            ).exit_code
            == 4
        )


def test_restart_replaces_one_pod_or_every_pod_and_waits(world: World) -> None:
    with up(world) as deployment:
        ns = deployment.namespace
        before = {p.uid for p in world.kube.pods(ns, {"app": "frontend"})}
        ctl(deployment, ["restart", "frontend"])
        after = {p.uid for p in world.kube.pods(ns, {"app": "frontend"})}
        assert len(after) == 2
        assert len(before & after) == 1
        assert all(p.ready for p in world.kube.pods(ns, {"app": "frontend"}))
        ctl(deployment, ["restart", "backend", "--every-pod"])
        assert not before & {p.uid for p in world.kube.pods(ns, {"app": "backend"})}
        with pytest.raises(DeployError, match=r"not in k8s.App\(restartable\)"):
            ctl(deployment, ["restart", "db"])


def test_flush_empties_every_pod_of_a_cache_only(world: World) -> None:
    with up(world) as deployment:
        ctl(deployment, ["flush", "cache"])
        assert [p.startswith("cache-") for p in world.kube.flushed(deployment.namespace)] == [True]
        with pytest.raises(DeployError, match=r"not in k8s.App\(caches\)"):
            ctl(deployment, ["flush", "frontend"])
        with pytest.raises(DeployError, match="want ctl restart"):
            ctl(deployment, ["reboot"])


def test_stop_and_start_scale_the_restartable_deployments(world: World) -> None:
    with up(world) as deployment:
        ctl(deployment, ["stop"])
        assert world.kube.pods(deployment.namespace, {"app": "frontend"}) == []
        assert len(world.kube.pods(deployment.namespace, {"app": "db"})) == 1
        ctl(deployment, ["start"])
        assert (
            len([p for p in world.kube.pods(deployment.namespace, {"app": "frontend"}) if p.ready])
            == 2
        )


def test_attach_acts_on_a_namespace_it_does_not_own(world: World) -> None:
    with up(world) as deployment:
        other = attach(
            world.source(make_app()), namespace=deployment.namespace, cluster=world.cluster
        )
        assert other.task_image == deployment.task_image
        ctl(other, ["restart", "frontend"])
        other.close()
        assert world.kube.live_namespaces() == [deployment.namespace]
        with pytest.raises(DeployError, match="not one of shop's"):
            attach(world.source(make_app()), namespace="hk8s-other-1", cluster=world.cluster)


def test_usage_reports_cpu_per_pod_between_snapshots(world: World) -> None:
    with up(world) as deployment:
        first = deployment.usage("closed")
        world.clock.sleep(10)
        second = deployment.usage("open")
        app_pods = {
            p.name
            for p in world.kube.pods(deployment.namespace)
            if p.labels.get("app") == "frontend"
        }
        assert app_pods <= set(second)
        assert all(second[name] == pytest.approx(5.0) for name in app_pods)
        assert all(first[name] > 0 for name in app_pods)
        assert (world.env.artifacts / "pod-usage-open.json").is_file()  # type: ignore[operator]


def _calls_of_a_full_run() -> int:
    with tempfile.TemporaryDirectory() as root:
        world = make_world(Path(root))
        with up(world):
            pass
        return len(world.kube.calls)


FULL_RUN = _calls_of_a_full_run()


@settings(max_examples=60, deadline=None)
@given(st.integers(1, FULL_RUN), st.sampled_from(["signal", "failure"]))
def test_an_exit_at_any_call_leaves_no_namespace(call: int, how: str) -> None:
    """Interrupt the deploy-and-teardown at any API call: the namespace is always deleted. After a
    signal the deletion is only requested (no wait, within the grace period)."""
    with tempfile.TemporaryDirectory() as root:
        world = make_world(Path(root))
        error: BaseException = (
            Stopped(15) if how == "signal" else KubeError("the API server went away")
        )
        world.kube.interrupt_at(call, error)
        try:
            with up(world):
                world.kube.programs["loadgen"]  # the sample's phases would run here
        except (Stopped, KubeError, DeployError):
            pass
        assert world.kube.live_namespaces() == []
        if how == "signal":  # at most a lookup of the UID and the deletion request after it
            assert len(world.kube.calls) - call <= 2
        world.clock.sleep(FakeKube.NAMESPACE_DELETE_SECS)
        assert world.kube.all_namespaces() == []
