from pathlib import Path

from tests.examples.request_factory_cpu_smoke import load_profile

from vibesys.api.request import load_input_bundle

_REPO_ROOT = Path(__file__).parents[2]
_BUNDLE_ROOT = _REPO_ROOT / "examples" / "model-serving" / "deepseek-v3.2-8xb200"
_BENCHMARK = _REPO_ROOT / "resources" / "evaluators" / "request-factory" / "fixed_text.py"


def test_bundle_uses_request_factory_adapter_and_result_protocol_v2() -> None:
    bundle = load_input_bundle(_BUNDLE_ROOT)

    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert bundle.manifest.benchmark.entrypoint == "request-factory-fixed-text-v1"
    assert bundle.manifest.benchmark.args == (
        "--model",
        "deepseek-ai/DeepSeek-V3.2",
        "--tokenizer",
        "deepseek-ai/DeepSeek-V3.2",
        "--tokenizer-revision",
        "a7e62ac04ecb2c0a54d736dc46601c5606cf10a6",
        "--request-count",
        "256",
        "--input-tokens",
        "8192",
        "--output-tokens",
        "1024",
        "--concurrency",
        "64",
    )
    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"


def test_objectives_match_the_metrics_declared_by_the_adapter() -> None:
    objectives = (_BUNDLE_ROOT / "objectives.toml").read_text(encoding="utf-8")

    assert 'name = "output_token_throughput_per_s"' in objectives
    assert 'name = "p90_latency_ms"' in objectives


def test_cpu_smoke_profile_declares_the_expected_request_and_result_contract() -> None:
    profile = load_profile(_BUNDLE_ROOT / "benchmark" / "cpu_smoke.toml")

    assert profile.benchmark_path == _BENCHMARK
    assert profile.shape_counts == {(16, 4): 8}
    assert set(profile.metrics) == {"output_token_throughput_per_s", "p90_latency_ms"}
    assert profile.required_fields["stream_options"] == {"include_usage": True}
    assert profile.unique_prompt_tokens is True
    assert profile.tokenizer_path.name == "cpu_smoke_tokenizer.json"
    assert {failure.mode for failure in profile.failures} == {
        "http",
        "malformed-sse",
        "truncated-sse",
        "output-mismatch",
    }
