from __future__ import annotations

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


def test_missing_assets_fail_with_the_flag_and_env_var(
    run: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(_ASSETS_ENV, raising=False)
    engine = tmp_path / "session_runner"

    assert run.main(["--mode", "smoke", "--request-factory-engine", str(engine)]) == 1
    error = capsys.readouterr().err
    assert "--trace" in error
    assert _ASSETS_ENV in error

    (tmp_path / "coding_session_synthetic.csv").write_text("")
    monkeypatch.setenv(_ASSETS_ENV, str(tmp_path))
    assert run.main(["--mode", "quick", "--request-factory-engine", str(engine)]) == 1
    assert f"--text-file does not exist: {tmp_path / 'corpus.txt'}" in capsys.readouterr().err


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
