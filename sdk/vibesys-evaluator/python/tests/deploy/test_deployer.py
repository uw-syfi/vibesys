"""KubeDeployer, KubePhaseRunner and the fault actions: the wrapper over the Go core.

The core is a stand-in (`FakeCore`); what it would do on a cluster is tested in Go.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibesys_evaluator._private import core
from vibesys_evaluator._private.files import DelimitedReader
from vibesys_evaluator._private.sample import Stopped
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices import get
from vibesys_evaluator.microservices._k8s import (
    KubeDeployer,
    PodOutOfMemoryError,
    ReplacedPod,
    attach,
    probe_plan,
    run_control,
)
from vibesys_evaluator.microservices._k8s._deployment import call
from vibesys_evaluator.microservices._private.deploy import (
    PHASE_INPUT_FILE,
    PHASE_LOG_FILE,
    DeployContext,
    DeployError,
    PhaseJob,
    PhaseRunError,
    TaskCommand,
)

from .k8s_world import TARGET, FakeCore, make_app


def context(tmp_path: Path, lines: list[str] | None = None) -> DeployContext:
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(exist_ok=True)
    task_dir = tmp_path / "task"
    task_dir.mkdir(exist_ok=True)
    return DeployContext(
        project_root=tmp_path / "project",
        artifacts=artifacts,
        task=TaskCommand(python="python", script=str(task_dir / "bench.py")),
        dataset_digest="d" * 64,
        log=(lines if lines is not None else []).append,
    )


def job() -> PhaseJob:
    return PhaseJob(name="phase-0", phase_input=b"in", timeout_secs=300)


def test_deploy_asks_the_core_and_phases_run_in_pods_with_their_files(
    fake_core: FakeCore, tmp_path: Path
) -> None:
    fake_core.replies["run-pod"] = {
        "exit_code": 0,
        "log": "ran\n",
        "seconds": 3.0,
        "__outputs__": {"output.binpb": "out", "results.binpb.gz": "r"},
    }
    fake_core.save()
    app = make_app(ready=[get("/")])
    out = tmp_path / "phase-0"
    out.mkdir()
    ctx = context(tmp_path)
    with KubeDeployer(app).deploy(ctx) as runner:
        runner.run_phase(job(), out)
        assert (ctx.artifacts / "namespace.txt").read_text() == "hk8s-shop-t0\n"
    assert fake_core.verbs() == ["deploy", "run-pod", "usage", "teardown"]

    deploy = fake_core.request("deploy")
    assert deploy["app"] == {
        "name": "shop",
        "manifest": "k8s/manifest.yaml",
        "images": {"APP_IMAGE": "app"},
        "gateway": "http://frontend:5000",
        "data_tier": "tier=data",
        "has_seed": True,
        "restartable": ["frontend", "backend"],
        "caches": {"cache": "K8S_CACHE_KIND_MEMCACHED"},
        "wait_secs": 300.0,
    }
    assert deploy["task_dir"] == str(tmp_path / "task")
    assert deploy["project_root"] == str(tmp_path / "project")
    assert deploy["artifacts_dir"] == str(ctx.artifacts)
    assert deploy["seed"] == {
        "argv": [
            "python",
            str(tmp_path / "task" / "bench.py"),
            "seed",
            "--dataset-digest",
            "d" * 64,
        ]
    }

    pod = fake_core.request("run-pod")
    assert pod["role"] == "K8S_ROLE_PHASE"
    assert pod["command"]["argv"] == [
        "python",
        str(tmp_path / "task" / "bench.py"),
        "phase",
        "--work",
        "/vse/work",
        "--target",
        "http://frontend:5000",
    ]
    assert pod["target"]["namespace"] == TARGET["namespace"]
    assert pod["timeout_secs"] == 300
    assert pod["pod_name"] == "vse-phase-1"
    assert pod["outputs_dir"] == str(out)
    assert Path(pod["command"]["input_files"][PHASE_INPUT_FILE]).name.endswith(PHASE_INPUT_FILE)
    assert fake_core.request("teardown") == {
        "target": TARGET,
        "fast": False,
        "failed": False,
    }
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [PHASE_LOG_FILE, "output.binpb", "results.binpb.gz"]
    )
    assert (out / PHASE_LOG_FILE).read_text() == "ran\n"


def test_the_apps_wait_goes_to_the_core(fake_core: FakeCore, tmp_path: Path) -> None:
    fake_core.save()
    with KubeDeployer(make_app(wait_secs=1800)).deploy(context(tmp_path)):
        pass
    assert fake_core.request("deploy")["app"]["wait_secs"] == 1800.0


def test_the_probe_plan_goes_to_the_core_as_a_file(fake_core: FakeCore, tmp_path: Path) -> None:
    plans: list[bytes] = []
    original = fake_core.binary.read_text()
    # The stand-in copies the plan file while it runs: the deploy's scratch directory is gone later.
    fake_core.binary.write_text(
        original.replace(
            "replies = json.loads",
            "import shutil\nif verb == 'deploy': shutil.copy(request['probe_plan_file'], str(here / 'plan.binpb'))\n"
            "replies = json.loads",
        )
    )
    del plans
    with KubeDeployer(make_app(ready=[get("/a")])).deploy(context(tmp_path)):
        pass
    reader = DelimitedReader(fake_core.root / "plan.binpb", pb.FileKind.REQUESTS)
    assert list(reader.one(pb.RequestsHeader).operation_names) == ["ready_0"]


def test_a_failed_phase_raises_phase_run_error_and_keeps_its_log(
    fake_core: FakeCore, tmp_path: Path
) -> None:
    fake_core.replies["run-pod"] = {"exit_code": 1, "log": "check failed\n", "seconds": 4.0}
    fake_core.save()
    out = tmp_path / "phase-0"
    out.mkdir()
    with (
        pytest.raises(PhaseRunError, match="phase-0 exited 1"),
        KubeDeployer(make_app(ready=[get("/")])).deploy(context(tmp_path)) as runner,
    ):
        runner.run_phase(job(), out)
    assert "check failed" in (out / PHASE_LOG_FILE).read_text()
    assert fake_core.request("teardown")["failed"] is True


def test_a_pod_that_cannot_run_is_a_phase_error_with_the_core_message(
    fake_core: FakeCore, tmp_path: Path
) -> None:
    fake_core.replies["run-pod"] = {
        "error_kind": "deploy",
        "message": "pod vse-phase-1 Failed, exit 2",
    }
    fake_core.save()
    out = tmp_path / "phase-0"
    out.mkdir()
    with (
        KubeDeployer(make_app(ready=[get("/")])).deploy(context(tmp_path)) as runner,
        pytest.raises(PhaseRunError, match="phase-0: pod vse-phase-1 Failed, exit 2"),
    ):
        runner.run_phase(job(), out)
    assert "Failed, exit 2" in (out / PHASE_LOG_FILE).read_text()


def test_an_oomkilled_phase_pod_names_the_limit_and_what_to_lower(
    fake_core: FakeCore, tmp_path: Path
) -> None:
    fake_core.replies["run-pod"] = {
        "error_kind": "out_of_memory",
        "message": "pod vse-phase-1 (x) was killed for running out of memory (OOMKilled at its 4Gi limit, exit 137)",
    }
    fake_core.save()
    out = tmp_path / "phase-1"
    out.mkdir()
    closed = phase_pb.PhaseInput(model=pb.LoadModel.CLOSED, clients=32, secs=60.0, warmup_secs=15.0)
    phase = PhaseJob(name="phase-1", phase_input=closed.to_binary(), timeout_secs=300)
    with (
        KubeDeployer(make_app(ready=[get("/")])).deploy(context(tmp_path)) as runner,
        pytest.raises(PhaseRunError) as raised,
    ):
        runner.run_phase(phase, out)
    message = str(raised.value)
    assert "ran out of memory (OOMKilled at its 4Gi limit)" in message
    assert "15 s warmup and 60 s window" in message
    assert "shrink the model or the dataset" in message
    assert "plan" not in message


def test_the_out_of_memory_error_carries_the_limit(fake_core: FakeCore) -> None:
    fake_core.replies["run-pod"] = {
        "error_kind": "out_of_memory",
        "message": "killed (OOMKilled at its 2Gi limit)",
    }
    fake_core.save()
    with pytest.raises(PodOutOfMemoryError) as raised:
        call("run-pod", {})
    assert raised.value.limit == "2Gi"


def test_a_failed_deploy_is_a_deploy_error_and_there_is_nothing_to_tear_down(
    fake_core: FakeCore, tmp_path: Path
) -> None:
    fake_core.replies["deploy"] = {"error_kind": "build", "message": "build of reg/x failed"}
    fake_core.save()
    with (
        pytest.raises(DeployError, match="build of reg/x failed"),
        KubeDeployer(make_app()).deploy(context(tmp_path)),
    ):
        pytest.fail("the body must not run")
    assert fake_core.verbs() == ["deploy"]


def test_a_signal_in_the_body_tears_down_fast(fake_core: FakeCore, tmp_path: Path) -> None:
    with pytest.raises(Stopped), KubeDeployer(make_app(ready=[get("/")])).deploy(context(tmp_path)):
        raise Stopped(15)
    assert fake_core.request("teardown") == {"target": TARGET, "fast": True, "failed": True}


def test_a_failing_teardown_does_not_hide_the_bodys_error(
    fake_core: FakeCore, tmp_path: Path
) -> None:
    fake_core.replies["teardown"] = {"error_kind": "kube", "message": "API gone"}
    fake_core.save()
    lines: list[str] = []
    with (
        pytest.raises(ValueError, match="body"),
        KubeDeployer(make_app(ready=[get("/")])).deploy(context(tmp_path, lines)),
    ):
        raise ValueError("body")
    assert any("API gone" in line for line in lines)
    with (
        pytest.raises(DeployError, match="API gone"),
        KubeDeployer(make_app(ready=[get("/")])).deploy(context(tmp_path)),
    ):
        pass


def test_the_probe_plan_is_a_recorded_stream_of_the_ready_steps(tmp_path: Path) -> None:
    path = tmp_path / "probe.binpb"
    path.write_bytes(probe_plan([get("/a"), get("/b", query={"x": "1"})]))
    reader = DelimitedReader(path, pb.FileKind.REQUESTS)
    header = reader.one(pb.RequestsHeader)
    operations = list(reader.rest(pb.PlannedOperation))
    assert list(header.operation_names) == ["ready_0", "ready_1"]
    assert [op.operation for op in operations] == [0, 1]


ENVIRON = {"VIBESYS_EVALUATOR_NAMESPACE": "hk8s-shop-t0"}


def test_ctl_needs_the_namespace_and_acts_on_it(fake_core: FakeCore, tmp_path: Path) -> None:
    app = make_app(ready=[get("/")])
    assert run_control(app, ["restart", "frontend"], environ={}, task_dir=tmp_path) == 2
    assert fake_core.calls == []
    assert run_control(app, ["flush", "cache"], environ=ENVIRON, task_dir=tmp_path) == 0
    assert fake_core.verbs() == ["attach", "flush"]
    assert fake_core.request("flush")["cache"] == "cache"
    assert fake_core.request("attach")["target"]["task_dir"] == str(tmp_path)

    fake_core.replies["flush"] = {"error_kind": "deploy", "message": "flush db: not in caches"}
    fake_core.save()
    assert run_control(app, ["flush", "db"], environ=ENVIRON, task_dir=tmp_path) == 1
    assert run_control(app, ["reboot"], environ=ENVIRON, task_dir=tmp_path) == 1


def test_ctl_maps_every_hook_to_a_verb(fake_core: FakeCore, tmp_path: Path) -> None:
    app = make_app(ready=[get("/")])
    for argv in (
        ["restart", "frontend"],
        ["restart", "backend", "--every-pod"],
        ["stop"],
        ["start"],
    ):
        assert run_control(app, argv, environ=ENVIRON, task_dir=tmp_path) == 0
    restarts = [c["request"] for c in fake_core.calls if c["verb"] == "restart"]
    assert [(r["deployments"], r["every_pod"], r["no_wait"]) for r in restarts] == [
        (["frontend"], False, False),
        (["backend"], True, False),
    ]
    assert fake_core.verbs().count("stop") == fake_core.verbs().count("start") == 1


def test_attach_checks_the_namespace_now(fake_core: FakeCore, tmp_path: Path) -> None:
    fake_core.replies["attach"] = {
        "error_kind": "deploy",
        "message": "ctl: namespace 'x' is not one of shop's",
    }
    fake_core.save()
    with pytest.raises(DeployError, match="not one of shop's"):
        attach(make_app(ready=[get("/")]), namespace="x", task_dir=tmp_path)


def test_a_missing_core_is_a_deploy_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_BUNDLED_DIR", tmp_path / "no-bin")
    monkeypatch.delenv(core.BIN_ENV, raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(DeployError, match="Go core failed"):
        attach(make_app(ready=[get("/")]), namespace="x", task_dir=tmp_path)


def test_fault_actions_are_core_calls(fake_core: FakeCore, tmp_path: Path) -> None:
    fake_core.replies["restart"] = {
        "replaced": [
            {"deployment": "frontend", "pod": "frontend-a", "uid": "u1"},
            {"deployment": "frontend", "pod": "frontend-b", "uid": "u2"},
        ]
    }
    fake_core.replies["exec"] = {
        "results": [
            {"pod": "cache-x", "output": "OK\n"},
            {"pod": "cache-y", "exit_code": 1, "output": "boom"},
        ]
    }
    fake_core.save()
    deployment = attach(make_app(ready=[get("/")]), namespace="hk8s-shop-t0", task_dir=tmp_path)
    assert deployment.namespace == "hk8s-shop-t0"

    replaced = deployment.restart("frontend", every_pod=True, wait=False)
    assert replaced == [
        ReplacedPod("frontend", "frontend-a", "u1"),
        ReplacedPod("frontend", "frontend-b", "u2"),
    ]
    restart = fake_core.request("restart")
    assert (restart["every_pod"], restart["no_wait"], restart["deployments"]) == (
        True,
        True,
        ["frontend"],
    )

    deployment.wait_ready(["frontend"], replaced=replaced, timeout_secs=30, probe=False)
    wait = fake_core.request("wait")
    assert wait["replaced_uids"] == ["u1", "u2"]
    assert (wait["deployments"], wait["timeout_secs"], wait["no_probe"]) == (["frontend"], 30, True)

    results = deployment.exec(["bash", "-c", "x"], deployment="cache", all_pods=True)
    assert [(r.pod, r.exit_code, r.output) for r in results] == [
        ("cache-x", 0, "OK\n"),
        ("cache-y", 1, "boom"),
    ]
    request = fake_core.request("exec")
    assert (request["deployment"], request["pod"], request["all_pods"]) == ("cache", "", True)
    assert request["argv"] == ["bash", "-c", "x"]
    with pytest.raises(DeployError, match=r"in cache-y exited 1: boom"):
        deployment.exec(["x"], deployment="cache", check=True)

    deployment.flush("cache")
    deployment.stop()
    deployment.start()
    assert fake_core.verbs()[-3:] == ["flush", "stop", "start"]


def test_an_action_the_core_refuses_is_a_deploy_error(fake_core: FakeCore, tmp_path: Path) -> None:
    fake_core.replies["restart"] = {
        "error_kind": "deploy",
        "message": "restart db: not in k8s.App(restartable) or caches (backend, frontend)",
    }
    fake_core.save()
    deployment = attach(make_app(ready=[get("/")]), namespace="hk8s-shop-t0", task_dir=tmp_path)
    with pytest.raises(DeployError, match=r"not in k8s.App\(restartable\)"):
        deployment.restart("db")
