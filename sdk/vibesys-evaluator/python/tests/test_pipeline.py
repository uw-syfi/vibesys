"""The whole sample, in process: main's benchmark command, phases, the pod's work, the records.

The deploy layer is the in-process Fake and loadgen the Fake loadgen, so the
files between the orchestrator, the phase pod and loadgen are the real ones,
in virtual time.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import vibesys_evaluator as vse

# test-isolation: main() exits and builds the k8s deployer; the sample's command is tested
# through its private entry, with the deploy seam (the interface the k8s layer implements).
from vibesys_evaluator._private.cli import Task, benchmark
from vibesys_evaluator._private.deploy import TaskCommand
from vibesys_evaluator._private.phase import PodTask
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vs_evaluator_protocol.api import Measurement, parse_records, read_measurement

from .vse_support import kv
from .vse_support.fake_loadgen import FakeLoadgen
from .vse_support.runners import InProcessDeployer, InProcessRunner

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

SCRIPT = TaskCommand(python="python3", script="/task/bench.py")


def _task(
    *,
    workload: kv.Workload[kv.Data, kv.Model] = kv.WORKLOAD,
    benchmark: Callable[[vse.Run], None] = kv.benchmark,
    metrics: Mapping[str, vse.Metric] = kv.METRICS,
    validity: vse.Validity = vse.Validity(),  # noqa: B008  # LW-150406 [B008]; a frozen dataclass
) -> Task:
    return Task(
        dataset=kv.Data,
        system=kv.SYSTEM,
        workload=workload,
        benchmark=benchmark,
        metrics=metrics,
        validity=validity,
    )


def _deployer(
    task: Task, service: kv.KvService, loadgen: FakeLoadgen | None = None
) -> InProcessDeployer:
    """The in-process deploy layer, with ``loadgen`` (a default Fake) against ``service``."""
    data = kv.Data()
    runner = InProcessRunner(
        task=PodTask(
            dataset=task.dataset,
            workload=task.workload,
            min_sustained_ratio=task.validity.min_sustained_ratio,
        ),
        loadgen=loadgen or FakeLoadgen(app=service.handle),
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


def test_the_second_phase_replays_the_first_phases_writes(tmp_path: Path) -> None:
    service = kv.KvService()
    deployer = _deployer(_task(), service)
    _sample(tmp_path, _task(), deployer)
    first, second = deployer.runner.jobs
    first_out = phase_pb.PhaseOutput.from_binary(
        (tmp_path / "artifacts/phase-0/output.binpb").read_bytes()
    )
    second_in = phase_pb.PhaseInput.from_binary(second.phase_input)
    assert phase_pb.PhaseInput.from_binary(first.phase_input).history == []
    assert len(first_out.writes) > 0
    assert list(second_in.history) == list(first_out.writes)
    assert second_in.phase_index == 1


def test_the_same_seed_sends_the_same_requests(tmp_path: Path) -> None:
    runs = []
    for n in range(2):
        service = kv.KvService()
        deployer = _deployer(_task(), service)
        _sample(tmp_path / str(n), _task(), deployer, seed=99)
        runs.append([j.phase_input for j in deployer.runner.jobs])
    assert runs[0] == runs[1]


def test_lost_writes_fail_the_sample_naming_the_check(tmp_path: Path) -> None:
    service = kv.KvService(lose_writes=True)
    code, text = _sample(tmp_path, _task(), _deployer(_task(), service))
    message = _error(text)
    assert code == 1
    assert "phase 0" in message
    assert "failed checks" in message
    assert "read #" in message


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
    deployer = _deployer(
        _task(), service := kv.KvService(), FakeLoadgen(app=service.handle, cpu_fraction=0.95)
    )
    _, text = _sample(tmp_path, _task(), deployer)
    assert "may be the bottleneck" in _error(text)


def test_late_sending_fails_an_open_loop(tmp_path: Path) -> None:
    deployer = _deployer(
        _task(), service := kv.KvService(), FakeLoadgen(app=service.handle, send_lag_micros=5000)
    )
    _, text = _sample(tmp_path, _task(), deployer)
    message = _error(text)
    assert "phase 1" in message
    assert "sent late" in message


def _closed_only(run: vse.Run) -> None:
    phase = run.closed_loop(clients=4, secs=1, warmup=0.5, max_ops_per_sec=50)
    run.report({"operations_per_second": phase.ops_per_sec, "latency_ms.p99": phase.p99_ms})


def test_an_exhausted_closed_loop_is_retried_once_with_a_larger_plan(tmp_path: Path) -> None:
    task = _task(benchmark=_closed_only)
    deployer = _deployer(task, kv.KvService())
    code, text = _sample(tmp_path, task, deployer)
    first, second = (phase_pb.PhaseInput.from_binary(j.phase_input) for j in deployer.runner.jobs)
    assert code == 0, text
    assert first.max_ops_per_sec == 50
    # Sized from the rate it sent at until the plan ran out: 4 clients at 20 ms an operation.
    assert second.max_ops_per_sec >= 2.0 * 200 * 0.95
    assert second.phase_index == 1
    assert len(second.history) > 0  # the exhausted attempt's writes stay in the history


def test_a_plan_exhausted_twice_fails_naming_the_bound(tmp_path: Path) -> None:
    task = _task(benchmark=_closed_only)
    service = kv.KvService()
    speeding_up = FakeLoadgen(app=service.handle, step_micros_by_run=[10_000, 500])
    deployer = _deployer(task, service, speeding_up)
    code, text = _sample(tmp_path, task, deployer)
    message = _error(text)
    assert code == 1
    assert "twice" in message
    assert "raise max_ops_per_sec" in message


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
    def bench(run: vse.Run) -> None:
        run.report(values)

    task = _task(benchmark=bench)
    _, text = _sample(tmp_path, task, _deployer(task, kv.KvService()))
    assert expected in _error(text)


def test_a_benchmark_that_never_reports_is_an_error(tmp_path: Path) -> None:
    task = _task(benchmark=lambda _run: None)
    _, text = _sample(tmp_path, task, _deployer(task, kv.KvService()))
    assert "without calling run.report" in _error(text)


def test_reporting_twice_is_an_error(tmp_path: Path) -> None:
    def bench(run: vse.Run) -> None:
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


def test_operations_never_sent_are_errors_and_not_writes(tmp_path: Path) -> None:
    # Every fifth operation is not sent: 80% sustained, 20% errors.
    lenient = _task(validity=vse.Validity(max_error_rate=0.5, min_sustained_ratio=0.5))
    deployer = _deployer(
        lenient, service := kv.KvService(), FakeLoadgen(app=service.handle, never_sent_every=5)
    )
    code, text = _sample(tmp_path, lenient, deployer)
    assert code == 0, text
    output = phase_pb.PhaseOutput.from_binary(
        (tmp_path / "artifacts/phase-1/output.binpb").read_bytes()
    )
    assert output.summary is not None
    assert output.summary.errors_by_kind.get(int(pb.ErrorKind.CANCELLED), 0) > 0
    assert output.writes
    assert all(w.start_unix_nanos != 0 for w in output.writes)
