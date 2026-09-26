from __future__ import annotations

from pathlib import Path

from tests.examples.request_factory_cpu_smoke import load_profile

from vibesys.api.request import load_input_bundle

_BUNDLE = Path(__file__).parents[2] / "examples" / "model-serving" / "Llama-3-8B"


def test_llama3_8b_uses_request_factory_and_declares_pareto_metrics() -> None:
    bundle = load_input_bundle(_BUNDLE)

    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert bundle.manifest.benchmark.entrypoint == "request-factory-adapter"
    assert bundle.manifest.benchmark.args == ("benchmark/benchmark.py",)
    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"

    objectives = (_BUNDLE / "objectives.toml").read_text(encoding="utf-8")
    assert 'name = "aggregate_throughput"' in objectives
    assert 'name = "p99_latency_ms"' in objectives


def test_llama3_8b_cpu_smoke_profile_declares_benchmark_contract() -> None:
    profile = load_profile(_BUNDLE / "benchmark" / "cpu_smoke.toml")

    assert profile.benchmark_path == _BUNDLE / "benchmark" / "benchmark.py"
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
