from pathlib import Path

from vibesys.api.request import load_input_bundle

_REPO_ROOT = Path(__file__).parents[2]
_BUNDLE_ROOT = _REPO_ROOT / "examples" / "model-serving" / "deepseek-v3.2-8xb200"


def test_bundle_uses_request_factory_adapter_and_result_protocol_v2() -> None:
    bundle = load_input_bundle(_BUNDLE_ROOT)

    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert bundle.manifest.benchmark.entrypoint == "request-factory-adapter"
    assert bundle.manifest.benchmark.args == ("benchmark/benchmark.py",)
    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"


def test_objectives_match_the_metrics_declared_by_the_adapter() -> None:
    objectives = (_BUNDLE_ROOT / "objectives.toml").read_text(encoding="utf-8")

    assert 'name = "output_token_throughput_per_s"' in objectives
    assert 'name = "p90_latency_ms"' in objectives
