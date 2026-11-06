"""The whole sample, in process: main's benchmark command, phases, the pod's files, the records.

The deploy layer is the in-process Fake and loadgen the Fake loadgen, so the
files between the orchestrator, the phase pod and loadgen are the real ones,
in virtual time; the core judges each phase (``phase judge``).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, Unpack

import pytest

import vibesys_evaluator as vse
from vibesys_evaluator import microservices as ms

# test-isolation: main() exits and builds the k8s deployer; the sample's command is tested
# through its private entry, with the deploy seam (the interface the k8s layer implements).
from vibesys_evaluator._private.files import read_records, read_results
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices._private.deploy import TaskCommand
from vibesys_evaluator.microservices._private.entry import Task, benchmark
from vs_evaluator_protocol.api import Measurement, parse_records, read_measurement

from .vse_support import kv
from .vse_support.fake_loadgen import FakeLoadgen
from .vse_support.runners import InProcessDeployer, InProcessGenerator, InProcessRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

SCRIPT = TaskCommand(python="python3", script="/task/bench.py")


def _task(
    *,
    workload: kv.Workload[kv.Data, kv.Model] = kv.WORKLOAD,
    benchmark: Callable[[ms.Run], None] = kv.benchmark,
    metrics: Mapping[str, vse.Metric] = kv.METRICS,
    validity: vse.Validity = vse.Validity(),  # noqa: B008  # LW-150406 [B008]; a frozen dataclass
    capture: bool = False,
) -> Task:
    return Task(
        dataset=kv.Data,
        system=kv.SYSTEM,
        workload=workload,
        benchmark=benchmark,
        metrics=metrics,
        validity=validity,
        capture=capture,
    )


class _Fake(TypedDict, total=False):
    """Options of the Fake loadgen."""

    cpu_fraction: float
    send_lag_micros: int
    never_sent_every: int
    generator_waited: int
    step_micros: int
    senders_saturated: bool


def _deployer(task: Task, service: kv.KvService, **fake: Unpack[_Fake]) -> InProcessDeployer:
    """The in-process deploy layer, with a Fake loadgen (``fake``: its options) against ``service``."""
    data = kv.Data()
    runner = InProcessRunner(
        loadgen=FakeLoadgen(app=service.handle, generator=InProcessGenerator(task), **fake),
        min_sustained_ratio=task.validity.min_sustained_ratio,
    )
    return InProcessDeployer(runner=runner, seed=lambda: kv.seed_service(service, data))


def _sample(
    tmp_path: Path, task: Task, deployer: InProcessDeployer, *, seed: int = 5
) -> tuple[int, str]:
    output = tmp_path / "records.jsonl"
    code = benchmark(
        task,
        project_root=tmp_path,
        output=output,
        seed_flag=seed,
        deployer=deployer,
        env={"VIBESYS_ARTIFACTS_DIR": str(tmp_path / "artifacts")},
        script=SCRIPT,
    )
    return code, output.read_text()


def _read(text: str) -> Measurement:
    """The stream as vibesys reads it (the protocol's reference reader)."""
    return read_measurement(parse_records(text))


def _error(text: str) -> str:
    measurement = _read(text)
    assert measurement.failure is not None, text
    return measurement.failure


def test_a_sample_reports_both_metrics_in_a_valid_stream(tmp_path: Path) -> None:
    service = kv.KvService()
    deployer = _deployer(_task(), service)
    code, text = _sample(tmp_path, _task(), deployer)
    measurement = _read(text)
    assert code == 0
    assert measurement.values is not None
    assert set(measurement.values) == {"operations_per_second", "latency_ms.p99"}
    assert measurement.values["operations_per_second"] > 0
    assert deployer.torn_down == 1
    artifacts = tmp_path / "artifacts"
    assert json.loads((artifacts / "seeds.json").read_text()) == {
        "sample_seed": 5,
        "source": "--seed",
    }
    assert deployer.contexts[0].dataset_digest  # the seed command is given the dataset's digest
    assert (artifacts / "dataset.json").is_file()
    assert sorted(p.name for p in artifacts.iterdir() if p.is_dir()) == ["phase-0", "phase-1"]
    assert {p.name for p in (artifacts / "phase-0").iterdir()} >= {
        "output.binpb",
        "summary.binpb",
        "results.binpb.gz",
    }


def test_every_phase_gets_the_sample_seed_and_its_index(tmp_path: Path) -> None:
    service = kv.KvService()
    deployer = _deployer(_task(), service)
    _sample(tmp_path, _task(), deployer)
    inputs = [phase_pb.PhaseInput.from_binary(job.phase_input) for job in deployer.runner.jobs]
    assert [(p.sample_seed, p.phase_index, p.capture) for p in inputs] == [
        (5, 0, False),
        (5, 1, False),
    ]
    assert [p.model for p in inputs] == [pb.LoadModel.CLOSED, pb.LoadModel.OPEN]


def test_a_benchmark_keeps_no_bodies_and_no_history(tmp_path: Path) -> None:
    code, _ = _sample(tmp_path, _task(), _deployer(_task(), kv.KvService()))
    assert code == 0
    artifacts = tmp_path / "artifacts"
    assert not (artifacts / "history.binpb").exists()
    assert not (artifacts / "phase-0" / "kept.binpb").exists()


def _capture(run: ms.Run) -> None:
    closed = run.closed_loop(clients=4, secs=1, warmup=0.5)
    restarts: list[str] = []
    run.fault("restart", "kv", lambda: restarts.append("kv"))
    opened = run.open_loop(rate=0.5 * closed.ops_per_sec, secs=1, warmup=0.5, arrivals="uniform")
    assert restarts == ["kv"]
    run.report({"operations_per_second": closed.ops_per_sec, "latency_ms.p99": opened.p99_ms})


def test_capture_records_the_history_an_accuracy_check_reads(tmp_path: Path) -> None:
    task = _task(benchmark=_capture, capture=True)
    code, text = _sample(tmp_path, task, _deployer(task, kv.KvService()))
    assert code == 0, text
    artifacts = tmp_path / "artifacts"
    manifest = phase_pb.HistoryManifest.from_binary((artifacts / "history.binpb").read_bytes())
    assert manifest.phases == 2
    assert manifest.dataset == "dataset.json"
    assert (artifacts / "dataset.json").is_file()
    [fault] = manifest.faults
    assert (fault.kind, fault.target, fault.after_phase) == ("restart", "kv", 0)
    assert fault.end_unix_nanos >= fault.start_unix_nanos > 0
    for index in range(2):
        phase_dir = artifacts / f"phase-{index}"
        results = read_results(phase_dir / "results.binpb")
        kept = {k.index: k for k in read_records(phase_dir / "kept.binpb", phase_pb.KeptOperation)}
        assert results
        for result in results:
            assert result.client is not None
            assert kept[result.index].operation == result.operation
            info = json.loads(kept[result.index].info_json)
            assert "key" in info
            if result.operation == 1:  # a write: begin_phase told the model the phase
                assert info["phase"] == index
            responses = [step.response for step in result.steps if step.response is not None]
            assert all(r.field == "body" for r in responses)  # every body is kept


def test_a_fault_goes_between_phases_and_is_recorded_when_it_fails(tmp_path: Path) -> None:
    def early(run: ms.Run) -> None:
        run.fault("restart", "kv", lambda: None)

    task = _task(benchmark=early, capture=True)
    _, text = _sample(tmp_path, task, _deployer(task, kv.KvService()))
    assert "a fault goes between phases" in _error(text)

    def failing(run: ms.Run) -> None:
        run.closed_loop(clients=4, secs=1, warmup=0.5)

        def action() -> None:
            msg = "no such pod"
            raise RuntimeError(msg)

        run.fault("restart", "kv", action)

    task = _task(benchmark=failing, capture=True)
    _, text = _sample(tmp_path / "failing", task, _deployer(task, kv.KvService()))
    assert "no such pod" in _error(text)
    manifest = phase_pb.HistoryManifest.from_binary(
        (tmp_path / "failing/artifacts/history.binpb").read_bytes()
    )
    assert [f.kind for f in manifest.faults] == ["restart"]


def test_the_same_seed_sends_the_same_requests(tmp_path: Path) -> None:
    runs = []
    for n in range(2):
        service = kv.KvService()
        deployer = _deployer(_task(), service)
        _sample(tmp_path / str(n), _task(), deployer, seed=99)
        runs.append([j.phase_input for j in deployer.runner.jobs])
    assert runs[0] == runs[1]


def test_status_errors_count_against_the_error_rate(tmp_path: Path) -> None:
    service = kv.KvService(fail_every=50)
    _, text = _sample(tmp_path, _task(), _deployer(_task(), service))
    assert "loadgen errors (status" in _error(text)
    lenient = _task(validity=vse.Validity(max_error_rate=0.5))
    code, text = _sample(
        tmp_path / "lenient", lenient, _deployer(lenient, kv.KvService(fail_every=50))
    )
    assert code == 0, text


def test_a_saturated_loadgen_fails_the_sample(tmp_path: Path) -> None:
    deployer = _deployer(_task(), kv.KvService(), cpu_fraction=0.95)
    _, text = _sample(tmp_path, _task(), deployer)
    assert "may be the bottleneck" in _error(text)


def test_late_sending_fails_an_open_loop(tmp_path: Path) -> None:
    deployer = _deployer(_task(), kv.KvService(), send_lag_micros=5000)
    _, text = _sample(tmp_path, _task(), deployer)
    message = _error(text)
    assert "phase 1" in message
    assert "sent late" in message


def test_late_sending_of_an_application_holding_every_sender_is_its_latency(
    tmp_path: Path,
) -> None:
    # Lag from senders all busy with slow responses is the application's queue, which the
    # latency (end - scheduled) already charges it for: not a loadgen verdict.
    deployer = _deployer(_task(), kv.KvService(), send_lag_micros=5000, senders_saturated=True)
    code, text = _sample(tmp_path, _task(), deployer)
    assert code == 0, text
    assert _read(text).values is not None


def test_busy_senders_do_not_excuse_a_generator_that_falls_behind(tmp_path: Path) -> None:
    # Saturation (every sender busy) is the application's latency; starvation (waiting for the
    # generator) is not, and still fails the sample, naming the generator's rate.
    deployer = _deployer(
        _task(), kv.KvService(), send_lag_micros=5000, senders_saturated=True, generator_waited=50
    )
    _, text = _sample(tmp_path, _task(), deployer)
    message = _error(text)
    assert "generator fell behind" in message
    assert "12345 ops/s" in message


def test_a_generator_that_falls_behind_fails_the_sample_naming_its_rate(tmp_path: Path) -> None:
    deployer = _deployer(_task(), kv.KvService(), generator_waited=50)
    _, text = _sample(tmp_path, _task(), deployer)
    message = _error(text)
    assert "phase 0" in message
    assert "generator fell behind" in message
    assert "12345 ops/s" in message


def test_a_closed_loop_reads_as_much_of_the_stream_as_its_clients_take(tmp_path: Path) -> None:
    fast = _task(benchmark=_closed_only)
    # 4 clients at 2 ms an operation: 3000 operations, far past the Fake's first read of the
    # stream; the phase is sized by nothing but its clients.
    deployer = _deployer(fast, kv.KvService(), step_micros=1000)
    code, text = _sample(tmp_path, fast, deployer)
    assert code == 0, text
    output = phase_pb.PhaseOutput.from_binary(
        (tmp_path / "artifacts/phase-0/output.binpb").read_bytes()
    )
    assert output.summary is not None
    assert output.summary.generator is not None
    assert output.summary.generator.read >= output.summary.completed >= 1_900


def test_the_generator_only_generates(tmp_path: Path) -> None:
    deployer = _deployer(_task(), kv.KvService())
    _sample(tmp_path, _task(), deployer)
    generator = deployer.runner.loadgen.generator
    assert isinstance(generator, InProcessGenerator)
    runs = generator.runs
    assert {run[0] for run in runs} == {"generate"}
    assert runs[0][runs[0].index("--dataset") + 1].endswith("dataset.json")


def _closed_only(run: ms.Run) -> None:
    phase = run.closed_loop(clients=4, secs=1, warmup=0.5)
    run.report({"operations_per_second": phase.ops_per_sec, "latency_ms.p99": phase.p99_ms})


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        pytest.param({"operations_per_second": 1.0}, "did not report", id="missing"),
        pytest.param(
            {"operations_per_second": 1.0, "latency_ms.p99": float("nan")}, "finite", id="nan"
        ),
        pytest.param(
            {"operations_per_second": 1.0, "latency_ms.p99": 1.0, "x": 2.0},
            "declared only",
            id="extra",
        ),
    ],
)
def test_a_bad_report_is_the_samples_error(
    tmp_path: Path, values: dict[str, float], expected: str
) -> None:
    def bench(run: ms.Run) -> None:
        run.report(values)

    task = _task(benchmark=bench)
    _, text = _sample(tmp_path, task, _deployer(task, kv.KvService()))
    assert expected in _error(text)


def test_a_benchmark_that_never_reports_is_an_error(tmp_path: Path) -> None:
    task = _task(benchmark=lambda _run: None)
    _, text = _sample(tmp_path, task, _deployer(task, kv.KvService()))
    assert "without calling run.report" in _error(text)


def test_reporting_twice_is_an_error(tmp_path: Path) -> None:
    def bench(run: ms.Run) -> None:
        values = {"operations_per_second": 1.0, "latency_ms.p99": 1.0}
        run.report(values)
        run.report(values)

    task = _task(benchmark=bench)
    _, text = _sample(tmp_path, task, _deployer(task, kv.KvService()))
    assert "report once" in _error(text)


def test_a_deploy_failure_is_an_error_after_hello_and_tears_nothing_down(tmp_path: Path) -> None:
    deployer = _deployer(_task(), kv.KvService())
    deployer.fail = "image build failed: no Dockerfile in server/"
    code, text = _sample(tmp_path, _task(), deployer)
    assert code == 1
    assert _error(text) == "image build failed: no Dockerfile in server/"
    assert _read(text).metrics is not None  # hello came first


def test_a_failing_build_names_the_operation(tmp_path: Path) -> None:
    def broken(_m: kv.Model, _rand: vse.Rand) -> kv.Request:
        missing = "no such user"
        raise KeyError(missing)

    workload = kv.Workload(
        model=kv.Model, operations=[kv.Operation(name="broken", weight=1, build=broken)]
    )
    task = _task(workload=workload)
    code, text = _sample(tmp_path, task, _deployer(task, kv.KvService()))
    assert code == 1
    assert "operation broken: build raised KeyError" in _error(text)


def test_bad_metric_names_are_refused_before_anything_runs(tmp_path: Path) -> None:
    task = _task(metrics={"p99 latency": vse.Metric(unit="ms", direction="min")})
    deployer = _deployer(task, kv.KvService())
    code, text = _sample(tmp_path, task, deployer)
    assert code == 1
    assert _read(text).metrics is None  # the error is the only record
    assert "whitespace" in _error(text)
    assert deployer.contexts == []


def test_the_sample_seed_comes_from_the_environment_when_no_flag_is_given(tmp_path: Path) -> None:
    output = tmp_path / "records.jsonl"
    env = {"VIBESYS_ARTIFACTS_DIR": str(tmp_path / "a"), "VIBESYS_SAMPLE_SEED": "0x2a"}
    benchmark(
        _task(),
        project_root=tmp_path,
        output=output,
        seed_flag=None,
        deployer=_deployer(_task(), kv.KvService()),
        env=env,
        script=SCRIPT,
    )
    assert json.loads((tmp_path / "a/seeds.json").read_text()) == {
        "sample_seed": 42,
        "source": "VIBESYS_SAMPLE_SEED",
    }


def test_a_malformed_sample_seed_is_an_error(tmp_path: Path) -> None:
    output = tmp_path / "records.jsonl"
    benchmark(
        _task(),
        project_root=tmp_path,
        output=output,
        seed_flag=None,
        deployer=_deployer(_task(), kv.KvService()),
        env={"VIBESYS_ARTIFACTS_DIR": str(tmp_path / "a"), "VIBESYS_SAMPLE_SEED": "soon"},
        script=SCRIPT,
    )
    assert "VIBESYS_SAMPLE_SEED" in _error(output.read_text())


def test_operations_never_sent_are_errors(tmp_path: Path) -> None:
    # Every fifth operation is not sent: 80% sustained, 20% errors.
    lenient = _task(validity=vse.Validity(max_error_rate=0.5, min_sustained_ratio=0.5))
    deployer = _deployer(lenient, kv.KvService(), never_sent_every=5)
    code, text = _sample(tmp_path, lenient, deployer)
    assert code == 0, text
    output = phase_pb.PhaseOutput.from_binary(
        (tmp_path / "artifacts/phase-1/output.binpb").read_bytes()
    )
    assert output.summary is not None
    assert output.summary.errors_by_kind.get(int(pb.ErrorKind.CANCELLED), 0) > 0
