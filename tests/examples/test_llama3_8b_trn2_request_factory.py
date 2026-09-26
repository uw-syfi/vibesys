from __future__ import annotations

from pathlib import Path

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
