from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vibesys.evaluators.input_manifest import load_input_bundle

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = Path(__file__).parents[2]
_BUNDLE = _REPO_ROOT / "examples" / "model-serving" / "qwen3.5-9b-mi210"
_ASSETS_ENV = "QWEN35_BENCH_ASSETS"


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import the bundle's stdlib-only benchmark/run.py as a module."""
    name = "qwen35_mi210_run"
    spec = importlib.util.spec_from_file_location(name, _BUNDLE / "benchmark" / "run.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve string annotations through sys.modules.
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_manifest_runs_quick_mode_through_the_adapter() -> None:
    bundle = load_input_bundle(_BUNDLE)
    manifest = bundle.manifest

    assert manifest.evaluator is not None
    assert manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert manifest.benchmark.entrypoint == "request-factory-adapter"
    assert manifest.benchmark.args == ("benchmark/run.py", "--mode", "quick")
    assert manifest.benchmark.result is not None
    assert manifest.benchmark.result.json_argument == "--output-json"
    assert manifest.benchmark.result.metric == "output_tokens_per_s"
    assert manifest.accuracy.command == ("uv", "run", "python", "accuracy_checker/checker.py")
    assert (_BUNDLE / "accuracy_checker" / "golden.json").is_file()


def _sessions(trace: Path) -> list[str]:
    rows = list(csv.DictReader(trace.open(newline="")))
    assert rows[0]["arrival_time_ms"] == "0.000000"
    return list(dict.fromkeys(row["session_id"] for row in rows))


def test_checked_in_slices_cover_every_mode_and_the_held_out_range(run: ModuleType) -> None:
    traces = _BUNDLE / "benchmark" / "traces"

    assert _sessions(run.DEFAULT_TRACE) == [f"synthetic_{i:06d}" for i in range(260)]
    assert max(run.MODE_SESSIONS.values()) <= 260
    assert run.WARMUP_SESSIONS <= 260
    held_out = _sessions(traces / "coding_session_3000-3299.csv")
    assert held_out == [f"synthetic_{i:06d}" for i in range(3000, 3300)]


def test_default_inputs_are_the_verified_slice_and_fetched_corpus(
    run: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(_ASSETS_ENV, raising=False)
    built = tmp_path / "corpus.txt"
    monkeypatch.setattr(run.fetch_corpus, "build", lambda _path: built)
    args = run.parse_args(["--mode", "quick", "--request-factory-engine", "rf"])

    assert run.resolve_trace(args) == run.DEFAULT_TRACE
    assert run.resolve_corpus(args) == built


def test_a_modified_default_trace_is_rejected(run: ModuleType, tmp_path: Path) -> None:
    tampered = tmp_path / "trace.csv"
    tampered.write_bytes(run.DEFAULT_TRACE.read_bytes().replace(b",306,", b",307,", 1))

    with pytest.raises(run.HarnessError, match=r"slice_trace\.py"):
        run.verify_default_trace(tampered)


def test_configured_inputs_must_exist(
    run: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.setenv(_ASSETS_ENV, str(tmp_path))
    engine = tmp_path / "session_runner"

    assert run.main(["--mode", "smoke", "--request-factory-engine", str(engine)]) == 1
    missing = tmp_path / "coding_session_synthetic.csv"
    assert f"--trace does not exist: {missing}" in capsys.readouterr().err


def test_unreachable_corpus_download_names_the_fallbacks(
    run: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(_ASSETS_ENV, raising=False)

    failure = run.fetch_corpus.CorpusError("GET failed")

    def offline(_path: Path) -> Path:
        raise failure

    monkeypatch.setattr(run.fetch_corpus, "build", offline)
    args = run.parse_args(["--mode", "quick", "--request-factory-engine", "rf"])

    with pytest.raises(run.HarnessError, match=r"fetch_corpus\.py") as error:
        run.resolve_corpus(args)
    assert _ASSETS_ENV in str(error.value)


def test_corpus_order_uses_only_pinned_ebooks(run: ModuleType) -> None:
    pinned = dict(run.fetch_corpus.EBOOKS)

    assert set(run.fetch_corpus.ORDER) == set(pinned)
    assert run.fetch_corpus.ORDER.count(1342) == 2


def test_measured_metrics_read_the_session_runner_summary(run: ModuleType) -> None:
    summary = {
        "replay": {
            "common": {
                "run_duration_ms": 2000.0,
                "actual_output_tokens": 400,
                "output_token_throughput_per_s": 200.0,
                "request_throughput_per_s": 3.0,
                "ttft_ms_p50": 10.0,
                "attempted_steps": 6,
                "success_steps": 6,
                "failed_steps": 0,
            },
            "prefix_cache": {
                "measured_server_prompt_tokens": 1600,
                "server_prefix_hit_rate": 0.5,
                "planned_prefix_hit_rate": 0.78,
            },
        }
    }

    metrics = run.measured_metrics(summary)

    assert metrics["output_tokens_per_s"] == 200.0
    assert metrics["total_tokens_per_s"] == 1000.0
    assert metrics["ttft_ms"]["p50"] == 10.0
    assert metrics["prefix_cache_hit_rate_server"] == 0.5
    assert metrics["failed_steps"] == 0
