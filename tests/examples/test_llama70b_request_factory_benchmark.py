from __future__ import annotations

import json
import sys
from pathlib import Path

from tests.support import run_test_command

from vibesys.api.request import load_input_bundle

_REPO_ROOT = Path(__file__).parents[2]
_BUNDLE_ROOT = _REPO_ROOT / "examples" / "model-serving" / "llama-70b-2xh100"
_BENCHMARK = _BUNDLE_ROOT / "benchmark" / "benchmark.py"


def test_bundle_runs_the_benchmark_through_the_pinned_rf_adapter() -> None:
    bundle = load_input_bundle(_BUNDLE_ROOT)

    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert bundle.manifest.benchmark.entrypoint == "request-factory-adapter"
    assert bundle.manifest.benchmark.args == ("benchmark/benchmark.py",)
    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"
    assert bundle.benchmark_command[-1] == "benchmark/benchmark.py"


def test_objectives_match_the_rf_protocol_metrics() -> None:
    objective_text = (_BUNDLE_ROOT / "objectives.toml").read_text(encoding="utf-8")

    assert 'name = "output_token_throughput_per_s"' in objective_text
    assert 'name = "p90_latency_ms"' in objective_text


def test_benchmark_rejects_an_undersized_request_factory_token_pool(tmp_path: Path) -> None:
    engine = tmp_path / "request-factory-warning"
    engine.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "print('warning: token pool (1 tokens) is smaller than the longest prompt', "
        "file=sys.stderr)\n",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    output = tmp_path / "result.jsonl"

    completed = run_test_command(
        [
            sys.executable,
            str(_BENCHMARK),
            "--request-factory-engine",
            str(engine),
            "--request-count",
            "1",
            "--input-tokens",
            "1",
            "--output-tokens",
            "1",
            "--concurrency",
            "1",
            "--vs-output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    record = json.loads(output.read_text(encoding="utf-8").splitlines()[-1])
    assert record["kind"] == "error"
    assert "token pool is shorter" in record["message"]
