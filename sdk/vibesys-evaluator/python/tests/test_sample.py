"""The core sample lifecycle: check, benchmark and profile modes, phases, records, errors.

A domain-free task (``vibesys_evaluator.main``) is driven through the core's
sample functions; the records are read back with the protocol's reference reader.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import vibesys_evaluator as vse

# test-isolation: main() exits the process and installs signal handlers; the lifecycle it runs
# is driven through its private entry points with an injected environment.
from vibesys_evaluator._private.run import new_run
from vibesys_evaluator._private.sample import (
    Mode,
    SampleError,
    benchmark_sample,
    unscored_sample,
)
from vs_evaluator_protocol.api import parse_records, read_measurement

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vibesys_evaluator._private.sample import Sample

METRICS = {"tokens_per_s": vse.Metric(unit="tok/s", direction="max")}


def _benchmark(tmp_path: Path, benchmark: Callable[[vse.Run], None]) -> tuple[int, str]:
    output = tmp_path / "records.jsonl"
    code = benchmark_sample(
        metrics=METRICS,
        output=output,
        seed_flag=7,
        env={"VIBESYS_ARTIFACTS_DIR": str(tmp_path / "artifacts")},
        body=lambda sample: benchmark(new_run(sample)),
    )
    return code, output.read_text()


def _unscored(tmp_path: Path, mode: Mode, function: Callable[[vse.Run], str | None]) -> int:
    def body(sample: Sample) -> str | None:
        return function(new_run(sample))

    return unscored_sample(
        mode, seed_flag=None, env={"VIBESYS_ARTIFACTS_DIR": str(tmp_path)}, body=body
    )


def test_a_benchmark_reports_its_metrics_and_keeps_its_phase_files(tmp_path: Path) -> None:
    def benchmark(run: vse.Run) -> None:
        (run.phase("load") / "trace.json").write_text("{}")
        run.report({"tokens_per_s": 12.5})

    code, text = _benchmark(tmp_path, benchmark)
    assert code == 0
    assert read_measurement(parse_records(text)).values == {"tokens_per_s": 12.5}
    assert (tmp_path / "artifacts/load/trace.json").is_file()
    assert (tmp_path / "artifacts/seeds.json").is_file()


@pytest.mark.parametrize(
    ("benchmark", "named"),
    [
        pytest.param(lambda _run: None, "without calling run.report", id="no-report"),
        pytest.param(
            lambda run: run.report({"other": 1.0}), "declared only ['tokens_per_s']", id="unknown"
        ),
        pytest.param(lambda run: run.phase("Bad Name"), "phase name", id="phase-name"),
        pytest.param(lambda run: (run.phase("a"), run.phase("a")), "used twice", id="phase-twice"),
        pytest.param(lambda _run: 1 / 0, "ZeroDivisionError", id="crash"),
    ],
)
def test_a_benchmark_that_is_not_a_measurement_ends_in_an_error_record(
    tmp_path: Path, benchmark: Callable[[vse.Run], None], named: str
) -> None:
    code, text = _benchmark(tmp_path, benchmark)
    measurement = read_measurement(parse_records(text))
    assert code == 1
    assert measurement.failure is not None
    assert named in measurement.failure


def test_an_expected_failure_is_reported_by_its_message(tmp_path: Path) -> None:
    def benchmark(_run: vse.Run) -> None:
        message = "the server never answered"
        raise SampleError(message)

    code, text = _benchmark(tmp_path, benchmark)
    assert code == 1
    assert read_measurement(parse_records(text)).failure == "the server never answered"


@pytest.mark.parametrize(
    ("verdict", "code"), [pytest.param(None, 0, id="pass"), pytest.param("wrong", 1, id="fail")]
)
def test_a_check_exits_with_its_verdict(tmp_path: Path, verdict: str | None, code: int) -> None:
    assert _unscored(tmp_path, Mode.CHECK, lambda _run: verdict) == code


def test_a_check_that_raises_fails(tmp_path: Path) -> None:
    def check(_run: vse.Run) -> str | None:
        message = "golden file missing"
        raise SampleError(message)

    assert _unscored(tmp_path, Mode.CHECK, check) == 1


def test_a_profile_is_never_scored(tmp_path: Path) -> None:
    reported: list[str] = []

    def profile(run: vse.Run) -> None:
        (run.phase("profile-load") / "done").write_text("")
        try:
            run.report({"tokens_per_s": 1.0})
        except ValueError as error:
            reported.append(str(error))

    assert _unscored(tmp_path, Mode.PROFILE, profile) == 0
    assert reported == ["run.report: the profile mode is never scored; report only in benchmark"]
    assert (tmp_path / "profile-load/done").is_file()
