from __future__ import annotations

from pathlib import Path

from tests.examples.request_factory_cpu_smoke import load_profile

from vibesys.api.request import load_input_bundle

_BUNDLE = Path(__file__).parents[2] / "examples" / "model-serving" / "Llama-3-8B-trn2"


def test_llama3_8b_trn2_uses_request_factory_protocol() -> None:
    bundle = load_input_bundle(_BUNDLE)

    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert bundle.manifest.benchmark.entrypoint == "request-factory-adapter"
    assert bundle.manifest.benchmark.args == ("benchmark/benchmark.py",)
    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"


def test_llama3_8b_trn2_cpu_smoke_profile_declares_benchmark_contract() -> None:
    profile = load_profile(_BUNDLE / "benchmark" / "cpu_smoke.toml")

    assert profile.benchmark_path == _BUNDLE / "benchmark" / "benchmark.py"
    assert profile.benchmark_args == (
        "--request-count",
        "4",
        "--lengths",
        "16,32",
        "--concurrencies",
        "1,2",
    )
    assert profile.shape_counts == {(16, 16): 8, (32, 32): 8}
    assert profile.response_style == "token-chunks"
    assert profile.required_fields == {"temperature": 0, "stream": True}
    assert profile.failure_message_contains == "success_steps"
    assert set(profile.metrics) == {"aggregate_throughput"}
