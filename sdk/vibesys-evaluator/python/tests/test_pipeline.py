"""The whole sample, in process: main's benchmark command, phases, the pod's work, the records.

The deploy layer is the in-process Fake and loadgen the Fake loadgen, so the
files between the orchestrator, the phase pod and loadgen are the real ones,
in virtual time.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, TypedDict, Unpack

import pytest

import vibesys_evaluator as vse
from vibesys_evaluator import microservices as ms
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator._proto.vibesys.evaluator.v1 import phase_pb
from vibesys_evaluator.microservices._private.deploy import TaskCommand

# test-isolation: main() exits and builds the k8s deployer; the sample's command is tested
# through its private entry, with the deploy seam (the interface the k8s layer implements).
from vibesys_evaluator.microservices._private.entry import Task, benchmark
from vibesys_evaluator.microservices._private.phase import PodTask
from vs_evaluator_protocol.api import Measurement, parse_records, read_measurement

from .vse_support import kv
from .vse_support.fake_loadgen import FakeLoadgen
from .vse_support.runners import GENERATOR, InProcessDeployer, InProcessGenerator, InProcessRunner

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
) -> Task:
    return Task(
        dataset=kv.Data,
        system=kv.SYSTEM,
        workload=workload,
        benchmark=benchmark,
        metrics=metrics,
        validity=validity,
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
    generator = InProcessGenerator(task)
    runner = InProcessRunner(
        task=PodTask(
            dataset=task.dataset,
            generator=GENERATOR,
            min_sustained_ratio=task.validity.min_sustained_ratio,
        ),
        loadgen=FakeLoadgen(app=service.handle, generator=generator, **fake),
        commands=generator,
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


def test_the_generator_runs_generate_under_loadgen_then_check(tmp_path: Path) -> None:
    deployer = _deployer(_task(), kv.KvService())
    _sample(tmp_path, _task(), deployer)
    commands = [run[0] for run in deployer.runner.commands.runs]
    assert commands[-1] == "check"
    assert set(commands) == {"generate", "check"}
    first = deployer.runner.commands.runs[0]
    assert first[first.index("--dataset") + 1].endswith("dataset.json")


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


def test_operations_never_sent_are_errors_and_not_writes(tmp_path: Path) -> None:
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
    assert output.writes
    assert all(w.start_unix_nanos != 0 for w in output.writes)
