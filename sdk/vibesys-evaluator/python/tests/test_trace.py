"""The trace command, in process: deploy, stitch around the trace body's phases, the reports.

The deploy layer is the in-process Fake (with a namespace, as the k8s deployer's runner has),
loadgen the Fake loadgen, and stitch a stand-in that records its arguments and writes the files
stitch promises (``vse_support/fake_stitch.py``); the core starts the sample and judges phases.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

import vibesys_evaluator as vse
from vibesys_evaluator import microservices as ms
from vibesys_evaluator._private import core

# test-isolation: main() exits and builds the k8s deployer; the trace command is tested through
# its private entry, with the deploy seam (the interface the k8s layer implements).
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices._private.deploy import TaskCommand
from vibesys_evaluator.microservices._private.entry import Task, benchmark, trace

from .vse_support import kv
from .vse_support.fake_loadgen import FakeLoadgen
from .vse_support.fake_stitch import RENDERED, FakeStitch
from .vse_support.runners import InProcessDeployer, InProcessGenerator, InProcessRunner

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from vibesys_evaluator.microservices._private.deploy import DeployContext

SCRIPT = TaskCommand(python="python3", script="/task/bench.py")
NAMESPACE = "vse-kv-test-t0"
SYSTEM = dataclasses.replace(kv.SYSTEM, restartable=("kv", "kv-auth"))
PAD = 1_000_000_000


def two_phases(run: ms.Run) -> None:
    """A closed loop, then an open loop: the trace's load."""
    run.closed_loop(clients=2, secs=1, warmup=0.5)
    run.open_loop(rate=20, secs=1, warmup=0.5, arrivals="uniform")


@dataclass
class NamespacedRunner:
    """The in-process runner, in a namespace (as the k8s deployer's runner is)."""

    inner: InProcessRunner
    namespace: str

    def run_phase(self, job: object, out_dir: Path) -> None:
        self.inner.run_phase(job, out_dir)  # ty: ignore[invalid-argument-type]


@dataclass
class NamespacedDeployer:
    inner: InProcessDeployer
    namespace: str = NAMESPACE
    failed: bool | None = None  # whether the deploy ended with an error (its teardown keeps diagnostics)

    @contextmanager
    def deploy(self, context: DeployContext) -> Iterator[NamespacedRunner]:
        with self.inner.deploy(context) as runner:
            try:
                yield NamespacedRunner(runner, self.namespace)
            except Exception:
                self.failed = True
                raise
            self.failed = False


@pytest.fixture
def stitch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeStitch:
    fake = FakeStitch(tmp_path / "stitch")
    monkeypatch.setattr(core, "_BUNDLED_DIR", tmp_path / "no-bin")
    monkeypatch.setenv(core.ENV_VARS["stitch"], str(fake.binary))
    return fake


def _task(
    trace_spec: ms.Trace | None,
    *,
    system: object = SYSTEM,
    validity: vse.Validity = vse.Validity(),  # noqa: B008  # LW-731726 [B008]; a frozen dataclass
) -> Task:
    return Task(
        dataset=kv.Data,
        system=system,  # ty: ignore[invalid-argument-type]
        workload=kv.WORKLOAD,
        benchmark=kv.benchmark,
        metrics=kv.METRICS,
        validity=validity,
        trace=trace_spec,
    )


def _in_process(task: Task) -> InProcessDeployer:
    service = kv.KvService()
    runner = InProcessRunner(
        loadgen=FakeLoadgen(app=service.handle, generator=InProcessGenerator(task)),
        min_sustained_ratio=task.validity.min_sustained_ratio,
    )
    return InProcessDeployer(runner=runner, seed=lambda: kv.seed_service(service, kv.Data()))


def _trace(tmp_path: Path, task: Task, deployer: object | None = None) -> tuple[int, Path]:
    artifacts = tmp_path / "artifacts"
    code = trace(
        task,
        project_root=tmp_path,
        out=None,
        seed_flag=5,
        deployer=deployer if deployer is not None else NamespacedDeployer(_in_process(task)),  # ty: ignore[invalid-argument-type]
        env={"VIBESYS_ARTIFACTS_DIR": str(artifacts)},
        script=SCRIPT,
    )
    return code, artifacts


def _windows(artifacts: Path) -> list[tuple[int, int]]:
    """Each phase's window, from the summary its pod left."""
    windows = []
    for name in ("phase-0", "phase-1"):
        output = phase_pb.PhaseOutput.from_binary((artifacts / name / "output.binpb").read_bytes())
        assert output.summary is not None
        windows.append(
            (output.summary.window_start_unix_nanos, output.summary.window_end_unix_nanos)
        )
    return windows


def test_a_trace_instruments_runs_the_phases_and_renders_the_reports(
    tmp_path: Path, stitch: FakeStitch
) -> None:
    deployer = NamespacedDeployer(_in_process(_task(ms.Trace(body=two_phases))))
    code, artifacts = _trace(tmp_path, _task(ms.Trace(body=two_phases)), deployer)
    assert code == 0
    assert stitch.verbs() == ["start", "collect", "ingest", "render"]
    assert deployer.inner.torn_down == 1
    out = artifacts / "trace"
    assert stitch.call("start") == [
        "capture",
        "otel-k8s",
        "start",
        "--namespace",
        NAMESPACE,
        "--deployment",
        "kv",
        "--deployment",
        "kv-auth",
        "--sampler-ratio",
        "1.0",
        "--timeout-seconds",
        "300",
    ]
    (w0, w1) = _windows(artifacts)
    assert stitch.call("collect") == [
        "capture",
        "otel-k8s",
        "collect",
        "--namespace",
        NAMESPACE,
        "--out",
        str(out / "capture"),
        "--settle-seconds",
        "5",
        "--window",
        f"{w0[0] - PAD}:{w0[1] + PAD}:phase-0",
        "--window",
        f"{w1[0] - PAD}:{w1[1] + PAD}:phase-1",
    ]
    assert stitch.call("ingest") == [
        "ingest",
        str(out / "capture" / "manifest.json"),
        "--out",
        str(out / "graph.json"),
    ]
    assert stitch.call("render") == ["render", str(out / "graph.json"), "--out", str(out)]
    assert json.loads((out / "start.json").read_text())["deployments"] == ["kv", "kv-auth"]
    for name in RENDERED:
        assert (out / name).is_file(), name
    assert (out / "README.txt").read_text() == "README.txt\n"  # stitch's own
    assert not (out / "capture" / "otel.jsonl").exists()
    with gzip.open(out / "capture" / "otel.jsonl.gz", "rt") as spans:
        assert len(spans.read().splitlines()) == 100
    log = (out / "stitch.log").read_text()
    assert log.count("$ stitch ") == 4
    assert "stitch: render working" in log  # each command's stderr
    assert not (artifacts / "records.jsonl").exists()


def test_the_trace_options_reach_stitch(tmp_path: Path, stitch: FakeStitch) -> None:
    spec = ms.Trace(body=two_phases, sampler_ratio=0.25, deployments=["frontend"], settle_secs=2)
    code, _ = _trace(tmp_path, _task(spec, system=dataclasses.replace(SYSTEM, wait_secs=42)))
    assert code == 0
    start = stitch.call("start")
    assert start[start.index("--sampler-ratio") + 1] == "0.25"
    assert start[start.index("--timeout-seconds") + 1] == "42"
    assert [start[i + 1] for i, a in enumerate(start) if a == "--deployment"] == ["frontend"]
    collect = stitch.call("collect")
    assert collect[collect.index("--settle-seconds") + 1] == "2"


def test_a_task_without_a_trace_exits_2_before_deploying(
    tmp_path: Path, stitch: FakeStitch
) -> None:
    deployer = NamespacedDeployer(_in_process(_task(None)))
    code, _ = _trace(tmp_path, _task(None), deployer)
    assert code == 2
    assert deployer.inner.contexts == []
    assert stitch.calls == []


def test_a_trace_without_deployments_to_instrument_exits_2(
    tmp_path: Path, stitch: FakeStitch, capsys: pytest.CaptureFixture[str]
) -> None:
    task = _task(ms.Trace(body=two_phases), system=kv.SYSTEM)  # no restartable Deployments
    code, _ = _trace(tmp_path, task)
    assert code == 2
    assert "Trace(deployments=" in capsys.readouterr().err
    assert stitch.calls == []


def test_a_failed_start_runs_no_load_and_says_why(tmp_path: Path, stitch: FakeStitch) -> None:
    stitch.fail("start")
    deployer = NamespacedDeployer(_in_process(_task(ms.Trace(body=two_phases))))
    code, artifacts = _trace(tmp_path, _task(ms.Trace(body=two_phases)), deployer)
    assert code == 1
    assert stitch.verbs() == ["start"]
    assert deployer.inner.runner.jobs == []
    assert deployer.inner.torn_down == 1
    assert deployer.failed  # torn down as failed: the pods' state and logs are kept
    readme = (artifacts / "trace" / "README.txt").read_text()
    assert readme.startswith("THE TRACE IS INCOMPLETE")
    assert "start failed on purpose" in readme


@pytest.mark.parametrize(
    ("verb", "ran"),
    [
        ("collect", ["start", "collect"]),
        ("ingest", ["start", "collect", "ingest"]),
        ("render", ["start", "collect", "ingest", "render"]),
    ],
)
def test_a_failed_step_keeps_what_was_written_and_exits_1(
    tmp_path: Path, stitch: FakeStitch, verb: str, ran: list[str]
) -> None:
    stitch.fail(verb)
    deployer = NamespacedDeployer(_in_process(_task(ms.Trace(body=two_phases))))
    code, artifacts = _trace(tmp_path, _task(ms.Trace(body=two_phases)), deployer)
    assert code == 1
    assert stitch.verbs() == ran
    assert len(deployer.inner.runner.jobs) == 2
    assert deployer.inner.torn_down == 1
    assert deployer.failed
    out = artifacts / "trace"
    readme = (out / "README.txt").read_text()
    assert readme.startswith("THE TRACE IS INCOMPLETE")
    assert f"{verb} failed on purpose" in readme
    assert (out / "stitch.log").is_file()
    if verb == "ingest":
        assert (out / "capture" / "otel.jsonl").is_file()  # the raw spans, kept for a retry
    if verb == "render":
        assert (out / "graph.json").is_file()
        assert (out / "capture" / "otel.jsonl.gz").is_file()
        assert "gunzip" in readme


def test_a_failing_body_is_still_collected_and_fails_the_trace(
    tmp_path: Path, stitch: FakeStitch
) -> None:
    def reports(run: ms.Run) -> None:
        run.closed_loop(clients=2, secs=1, warmup=0.5)
        run.report({"operations_per_second": 1.0, "latency_ms.p99": 1.0})

    code, artifacts = _trace(tmp_path, _task(ms.Trace(body=reports)))
    assert code == 1
    assert stitch.verbs() == ["start", "collect", "ingest", "render"]
    collect = stitch.call("collect")
    assert collect.count("--window") == 1
    readme = (artifacts / "trace" / "README.txt").read_text()
    assert "the trace body failed" in readme
    assert "never scored" in readme


def test_a_phase_that_is_not_a_measurement_is_traced_all_the_same(
    tmp_path: Path, stitch: FakeStitch
) -> None:
    strict = vse.Validity(min_ops_per_operation=10**9)
    code, _ = _trace(tmp_path, _task(ms.Trace(body=two_phases), validity=strict))
    assert code == 0
    assert stitch.call("collect").count("--window") == 2


def test_a_deploy_without_a_namespace_cannot_be_traced(tmp_path: Path, stitch: FakeStitch) -> None:
    task = _task(ms.Trace(body=two_phases))
    deployer = _in_process(task)
    code, artifacts = _trace(tmp_path, task, deployer)
    assert code == 1
    assert stitch.calls == []
    assert deployer.torn_down == 1
    assert "no namespace" in (artifacts / "trace" / "README.txt").read_text()


def test_a_missing_stitch_fails_before_deploying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(core, "_BUNDLED_DIR", tmp_path / "no-bin")
    monkeypatch.delenv(core.ENV_VARS["stitch"], raising=False)
    monkeypatch.setenv("PATH", str(tmp_path))
    task = _task(ms.Trace(body=two_phases))
    deployer = NamespacedDeployer(_in_process(task))
    with pytest.raises(core.CoreNotFoundError, match="VIBESYS_EVALUATOR_STITCH"):
        _trace(tmp_path, task, deployer)
    assert deployer.inner.contexts == []
    capsys.readouterr()


def test_a_benchmark_never_runs_stitch(tmp_path: Path, stitch: FakeStitch) -> None:
    task = _task(ms.Trace(body=two_phases))
    code = benchmark(
        task,
        project_root=tmp_path,
        output=tmp_path / "records.jsonl",
        seed_flag=5,
        deployer=NamespacedDeployer(_in_process(task)),
        env={"VIBESYS_ARTIFACTS_DIR": str(tmp_path / "artifacts")},
        script=SCRIPT,
    )
    assert code == 0
    assert stitch.calls == []
    assert not (tmp_path / "artifacts" / "trace").exists()


@pytest.mark.parametrize(
    ("options", "error"),
    [
        ({"sampler_ratio": 0}, ValueError),
        ({"sampler_ratio": 1.5}, ValueError),
        ({"sampler_ratio": True}, ValueError),
        ({"settle_secs": -1}, ValueError),
        ({"settle_secs": float("inf")}, ValueError),
        ({"deployments": []}, ValueError),
        ({"deployments": "frontend"}, ValueError),
        ({"deployments": ["Front_End"]}, ValueError),
        ({"body": "two_phases"}, TypeError),
    ],
)
def test_a_bad_trace_is_refused_naming_the_field(
    options: dict[str, object], error: type[Exception]
) -> None:
    fields: dict[str, object] = {"body": two_phases, **options}
    with pytest.raises(error, match="Trace"):
        ms.Trace(**fields)  # ty: ignore[invalid-argument-type]
