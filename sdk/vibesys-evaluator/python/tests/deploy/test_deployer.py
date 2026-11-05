"""KubeDeployer and KubePhaseRunner through the orchestrator's deploy interface, on the fake cluster."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from vibesys_evaluator._private.deploy import (
    PHASE_INPUT_FILE,
    PHASE_LOG_FILE,
    PHASE_OUTPUT_FILE,
    DeployContext,
    PhaseJob,
    PhaseRunError,
    TaskCommand,
)
from vibesys_evaluator._private.files import DelimitedReader
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator.deploy._k8s import WORK_DIR, KubeDeployer, Outcome, probe_plan, run_control
from vibesys_evaluator.service import get

from .k8s_world import World, make_app

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from vibesys_evaluator.deploy._k8s import Cluster, Environment

ENVIRON = {
    "KUBECONFIG": "/vibesys/kubeconfig",
    "VIBESYS_K8S_REGISTRY": "registry:5000",
    "VIBESYS_K8S_BUILDER": "tcp://builder:1234",
}


def connected(world: World) -> Callable[[Environment, Callable[[str], None]], Cluster]:
    def connect(env: Environment, log: Callable[[str], None]) -> Cluster:
        del log
        return dataclasses.replace(world.cluster, env=env)

    return connect


def context(world: World, artifacts: Path) -> DeployContext:
    return DeployContext(
        project_root=world.project_root,
        artifacts=artifacts,
        task=TaskCommand(python="python", script=str(world.task_dir / "bench.py")),
        dataset_digest="d" * 64,
        log=world.log,
    )


def test_phases_run_in_pods_with_their_files(world: World, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def program(argv: Sequence[str], inputs: Mapping[str, bytes]) -> Outcome:
        if "seed" in argv:
            seen["seed"] = tuple(argv)
            return Outcome(log="seeded\n")
        seen["phase"], seen["input"] = tuple(argv), inputs[PHASE_INPUT_FILE]
        return Outcome(outputs={PHASE_OUTPUT_FILE: b"out", "results.binpb.gz": b"r"}, log="ran\n")

    world.kube.programs["python"] = program
    app = make_app(ready=[get("/")])
    deployer = KubeDeployer(app, environ=ENVIRON, connect=connected(world))
    out = tmp_path / "phase-0"
    out.mkdir()
    with deployer.deploy(context(world, tmp_path)) as runner:
        runner.run_phase(PhaseJob(name="phase-0", phase_input=b"in", timeout_secs=300), out)
        namespace = (tmp_path / "namespace.txt").read_text().strip()
        assert world.kube.live_namespaces() == [namespace]
    assert world.kube.live_namespaces() == []
    assert seen["seed"] == (
        "python",
        str(world.task_dir / "bench.py"),
        "seed",
        "--dataset-digest",
        "d" * 64,
    )
    assert seen["phase"] == (
        "python",
        str(world.task_dir / "bench.py"),
        "phase",
        "--work",
        WORK_DIR,
        "--target",
        "http://frontend:5000",
    )
    assert seen["input"] == b"in"
    assert sorted(p.name for p in out.iterdir()) == sorted(
        [PHASE_LOG_FILE, PHASE_OUTPUT_FILE, "results.binpb.gz"]
    )
    assert (out / PHASE_LOG_FILE).read_text() == "ran\n"
    assert (tmp_path / "pod-usage-phase-0.json").is_file()


def test_a_failed_phase_raises_phase_run_error_and_keeps_its_log(
    world: World, tmp_path: Path
) -> None:
    world.kube.programs["python"] = lambda argv, _inputs: (
        Outcome() if "seed" in argv else Outcome(exit_code=1, log="check failed\n")
    )
    deployer = KubeDeployer(make_app(ready=[get("/")]), environ=ENVIRON, connect=connected(world))
    out = tmp_path / "phase-0"
    out.mkdir()
    with (
        deployer.deploy(context(world, tmp_path)) as runner,
        pytest.raises(PhaseRunError, match="phase-0 exited 1"),
    ):
        runner.run_phase(PhaseJob(name="phase-0", phase_input=b"in", timeout_secs=300), out)
    assert "check failed" in (out / PHASE_LOG_FILE).read_text()
    assert world.kube.live_namespaces() == []


def test_the_probe_plan_is_a_requests_file_of_the_ready_steps(tmp_path: Path) -> None:
    path = tmp_path / "probe.binpb"
    path.write_bytes(probe_plan([get("/a"), get("/b", query={"x": "1"})]))
    reader = DelimitedReader(path, pb.FileKind.REQUESTS)
    header = reader.one(pb.RequestsHeader)
    operations = list(reader.rest(pb.PlannedOperation))
    assert header.count == 2
    assert [op.operation for op in operations] == [0, 1]


def test_ctl_needs_the_namespace_and_acts_on_it(world: World, tmp_path: Path) -> None:
    app = make_app(ready=[get("/")])
    assert (
        run_control(
            app,
            ["restart", "frontend"],
            environ=ENVIRON,
            task_dir=world.task_dir,
            connect=connected(world),
        )
        == 2
    )
    world.kube.programs["python"] = lambda *_: Outcome()
    deployer = KubeDeployer(app, environ=ENVIRON, connect=connected(world))
    with deployer.deploy(context(world, tmp_path)):
        namespace = (tmp_path / "namespace.txt").read_text().strip()
        environ = {**ENVIRON, "VIBESYS_EVALUATOR_NAMESPACE": namespace}
        assert (
            run_control(
                app,
                ["flush", "cache"],
                environ=environ,
                task_dir=world.task_dir,
                connect=connected(world),
            )
            == 0
        )
        assert (
            run_control(
                app,
                ["flush", "db"],
                environ=environ,
                task_dir=world.task_dir,
                connect=connected(world),
            )
            == 1
        )
        assert len(world.kube.flushed(namespace)) == 1
