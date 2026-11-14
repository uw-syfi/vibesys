from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.examples.example_task import load_example_task, task_root
from tests.examples.request_factory_cpu_smoke import load_profile
from tests.support import run_test_command

from vs_evaluator_protocol.api import parse_records, read_measurement

if TYPE_CHECKING:
    import subprocess

_BUNDLE = Path(__file__).parents[2] / "examples" / "model-serving" / "Llama-3-8B"
_BENCHMARK = task_root(_BUNDLE) / "benchmark" / "benchmark.py"
_FAKE_ENGINE = Path(__file__).with_name("fakes") / "request_factory_sweep_engine.py"
_FAKE_MODULES = Path(__file__).with_name("fakes")
_TOKENIZER_REVISION = "0e9e39f249a16976918f6564b8830bc894c89659"
_DEFAULT_SWEEP = (1, 2, 4, 8, 16, 32, 64, 128)


def test_llama3_8b_uses_request_factory_and_declares_pareto_metrics() -> None:
    bundle = load_example_task(_BUNDLE)

    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert bundle.manifest.benchmark.entrypoint == "request-factory-adapter"
    assert bundle.manifest.benchmark.args == (
        "${PROJECT_ROOT}/.vibesys/tasks/default/benchmark/benchmark.py",
    )
    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"

    objectives = (task_root(_BUNDLE) / "objectives.toml").read_text(encoding="utf-8")
    assert 'name = "aggregate_throughput"' in objectives
    assert 'name = "p99_latency_ms"' in objectives


def test_llama3_8b_cpu_smoke_profile_declares_benchmark_contract() -> None:
    profile = load_profile(task_root(_BUNDLE) / "benchmark" / "cpu_smoke.toml")

    assert profile.benchmark_path == task_root(_BUNDLE) / "benchmark" / "benchmark.py"
    assert profile.benchmark_args == (
        "--request-count",
        "32",
        "--input-tokens",
        "16",
        "--output-tokens",
        "4",
        "--concurrencies",
        "1",
    )
    assert profile.shape_counts == {(16, 4): 64}
    assert profile.response_style == "token-chunks"
    assert profile.required_fields == {"temperature": 0, "stream": True}
    assert profile.failure_message_contains == "failed requests"
    assert set(profile.metrics) == {"aggregate_throughput", "p99_latency_ms"}
    assert profile.unique_prompts is True
    assert profile.tokenizer_path.name == "request_factory_tokenizer.json"


def test_default_tokenizer_resolves_an_exact_revision_before_rf(tmp_path: Path) -> None:
    tokenizer = tmp_path / "snapshot" / "tokenizer.json"
    tokenizer.parent.mkdir()
    tokenizer.write_text("{}", encoding="utf-8")
    download_capture = tmp_path / "download.json"
    rf_capture = tmp_path / "rf.jsonl"
    output = tmp_path / "result.jsonl"
    state = tmp_path / "state.json"
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            filter(None, (str(_FAKE_MODULES), os.environ.get("PYTHONPATH", "")))
        ),
        "HF_FAKE_CAPTURE": str(download_capture),
        "HF_FAKE_LOCAL_PATH": str(tokenizer),
        "RF_FAKE_POINTS": json.dumps({"1": [[100.0, 10.0], [100.0, 10.0]]}),
        "RF_FAKE_STATE": str(state),
        "RF_FAKE_CAPTURE": str(rf_capture),
    }

    completed = run_test_command(
        [
            sys.executable,
            _BENCHMARK,
            "--request-factory-engine",
            _FAKE_ENGINE,
            "--request-count",
            "1",
            "--input-tokens",
            "2",
            "--output-tokens",
            "1",
            "--concurrencies",
            "1",
            "--vs-output",
            output,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(download_capture.read_text(encoding="utf-8")) == {
        "repo_id": "meta-llama/Llama-3.1-8B-Instruct",
        "filename": "tokenizer.json",
        "revision": _TOKENIZER_REVISION,
    }
    invocations = [json.loads(line) for line in rf_capture.read_text(encoding="utf-8").splitlines()]
    assert {invocation["tokenizer"] for invocation in invocations} == {str(tokenizer)}


def _run_sweep(
    tmp_path: Path,
    points: dict[int, list[tuple[float, float]]],
    concurrencies: str | None,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    output = tmp_path / "result.jsonl"
    details = tmp_path / "details.json"
    state = tmp_path / "fake-state.json"
    capture = tmp_path / "captured-prompts.jsonl"
    environment = {
        **os.environ,
        "RF_FAKE_POINTS": json.dumps({str(key): value for key, value in points.items()}),
        "RF_FAKE_STATE": str(state),
        "RF_FAKE_CAPTURE": str(capture),
    }
    completed = run_test_command(
        [
            sys.executable,
            _BENCHMARK,
            "--request-factory-engine",
            _FAKE_ENGINE,
            "--tokenizer",
            Path(__file__).with_name("fixtures") / "request_factory_tokenizer.json",
            "--request-count",
            "4",
            "--input-tokens",
            "8",
            "--output-tokens",
            "4",
            *(() if concurrencies is None else ("--concurrencies", concurrencies)),
            "--output-json",
            details,
            "--vs-output",
            output,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    return completed, output, details, capture


def _captured_prompts(path: Path) -> list[tuple[int, ...]]:
    invocations = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [tuple(prompt) for invocation in invocations for prompt in invocation["prompts"]]


def test_sweep_confirms_overload_with_cache_distinct_prompts_and_valid_protocol(
    tmp_path: Path,
) -> None:
    completed, output, details, capture = _run_sweep(
        tmp_path,
        {
            1: [(100.0, 10.0), (110.0, 12.0)],
            2: [(108.0, 15.0)],
            4: [(90.0, 50.0), (92.0, 52.0)],
        },
        "1,4",
    )

    assert completed.returncode == 0, completed.stderr
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.values == {"aggregate_throughput": 108.0, "p99_latency_ms": 15.0}
    result = json.loads(details.read_text(encoding="utf-8"))
    assert result["selected_concurrency"] == 2
    assert [(point["concurrency"], point["repetitions"]) for point in result["sweep"]] == [
        (1, 2),
        (2, 1),
        (4, 2),
    ]
    assert result["sweep"][0]["aggregate_throughput"] == 105.0
    assert result["sweep"][0]["p99_latency_ms"] == 11.0
    prompts = _captured_prompts(capture)
    assert len(prompts) == 20
    assert len(set(prompts)) == len(prompts)


def test_sweep_confirms_best_and_neighbors_then_selects_one_aggregated_point(
    tmp_path: Path,
) -> None:
    completed, output, details, capture = _run_sweep(
        tmp_path,
        {
            1: [(80.0, 8.0), (82.0, 10.0)],
            2: [(100.0, 12.0), (106.0, 14.0)],
            4: [(99.0, 16.0), (97.0, 18.0)],
        },
        "1,2,4",
    )

    assert completed.returncode == 0, completed.stderr
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.values == {"aggregate_throughput": 103.0, "p99_latency_ms": 13.0}
    result = json.loads(details.read_text(encoding="utf-8"))
    assert result["selected_concurrency"] == 2
    assert [(point["concurrency"], point["repetitions"]) for point in result["sweep"]] == [
        (1, 2),
        (2, 2),
        (4, 2),
    ]
    selected = next(point for point in result["sweep"] if point["concurrency"] == 2)
    assert result["metrics"] == {
        "aggregate_throughput": selected["aggregate_throughput"],
        "p99_latency_ms": selected["p99_latency_ms"],
    }
    prompts = _captured_prompts(capture)
    assert len(prompts) == 24
    assert len(set(prompts)) == len(prompts)


def test_sweep_rejects_a_ceiling_that_is_still_rising_with_valid_error_protocol(
    tmp_path: Path,
) -> None:
    completed, output, _details, _capture = _run_sweep(
        tmp_path,
        {1: [(100.0, 10.0)], 2: [(104.0, 12.0)]},
        "1,2",
    )

    assert completed.returncode == 1
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.failed
    assert "still rising" in (measurement.failure or "")


_DEFAULT_SWEEP_MAX = 128
_CONCURRENCY_CAP = 1024
_REQUESTS_PER_DEFAULT_POINT = 4  # `_run_sweep` passes --request-count 4
_MIN_WAVES = 4


def _saturating_server(peak: int) -> dict[int, list[tuple[float, float]]]:
    """A server whose throughput scales linearly to ``peak`` concurrent requests, then flattens.

    Past the peak, p99 latency grows with the square of the overshoot, so the wrapper sees overload.
    Every concurrency a sweep might ask for has repeat observations ready.
    """
    return {
        concurrency: [(100.0 * min(concurrency, peak), 10.0 * max(1.0, concurrency / peak) ** 2)]
        * 3
        for concurrency in range(1, _CONCURRENCY_CAP + 1)
    }


def _captured_invocations(path: Path) -> list[tuple[int, list[tuple[int, ...]]]]:
    invocations = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [
        (invocation["concurrency"], [tuple(prompt) for prompt in invocation["prompts"]])
        for invocation in invocations
    ]


@pytest.mark.parametrize("peak", [64, 256, 512])
def test_default_sweep_extends_until_the_peak_is_found(tmp_path: Path, peak: int) -> None:
    completed, output, details, capture = _run_sweep(tmp_path, _saturating_server(peak), None)

    assert completed.returncode == 0, completed.stderr
    result = json.loads(details.read_text(encoding="utf-8"))
    assert result["selected_concurrency"] == peak
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.values == result["metrics"]
    swept = [point["concurrency"] for point in result["sweep"]]
    assert set(_DEFAULT_SWEEP) <= set(swept)
    # The sweep extends only as far as needed: one doubling past the peak, never beyond the cap.
    assert max(swept) == max(2 * peak, _DEFAULT_SWEEP_MAX)
    assert max(swept) <= _CONCURRENCY_CAP

    invocations = _captured_invocations(capture)
    for concurrency, prompts in invocations:
        expected = (
            _REQUESTS_PER_DEFAULT_POINT
            if concurrency <= _DEFAULT_SWEEP_MAX
            else _MIN_WAVES * concurrency
        )
        assert len(prompts) == expected, f"concurrency {concurrency}"
    all_prompts = [prompt for _concurrency, prompts in invocations for prompt in prompts]
    assert len(set(all_prompts)) == len(all_prompts)


def test_default_sweep_still_rising_at_the_cap_fails_with_valid_error_protocol(
    tmp_path: Path,
) -> None:
    completed, output, _details, capture = _run_sweep(tmp_path, _saturating_server(10**6), None)

    assert completed.returncode == 1
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.failed
    assert "still rising" in (measurement.failure or "")
    assert f"concurrency {_CONCURRENCY_CAP}" in (measurement.failure or "")
    highest = max(concurrency for concurrency, _prompts in _captured_invocations(capture))
    assert highest == _CONCURRENCY_CAP


def test_explicit_sweep_is_never_extended_or_rescaled(tmp_path: Path) -> None:
    completed, output, _details, capture = _run_sweep(
        tmp_path, _saturating_server(10**6), "64,128,256"
    )

    assert completed.returncode == 1
    measurement = read_measurement(parse_records(output.read_text(encoding="utf-8")))
    assert measurement.failed
    assert "still rising" in (measurement.failure or "")
    invocations = _captured_invocations(capture)
    assert [concurrency for concurrency, _prompts in invocations] == [64, 128, 256]
    assert {len(prompts) for _concurrency, prompts in invocations} == {_REQUESTS_PER_DEFAULT_POINT}