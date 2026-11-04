from __future__ import annotations

from pathlib import Path

from tests.examples.example_task import load_example_task, task_root
from tests.examples.request_factory_cpu_smoke import load_profile

_REPO_ROOT = Path(__file__).parents[2]
_BUNDLE_ROOT = _REPO_ROOT / "examples" / "model-serving" / "qwen3.6-35b-a3b"
_BENCHMARK = _REPO_ROOT / "resources" / "evaluators" / "request-factory" / "fixed_text.py"


def test_bundle_runs_the_benchmark_through_the_pinned_rf_adapter() -> None:
    bundle = load_example_task(_BUNDLE_ROOT)

    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert bundle.manifest.benchmark.entrypoint == "request-factory-fixed-text-v1"
    assert bundle.manifest.benchmark.args == (
        "--model",
        "Qwen/Qwen3.6-35B-A3B",
        "--tokenizer",
        "Qwen/Qwen3.6-35B-A3B",
        "--tokenizer-revision",
        "995ad96eacd98c81ed38be0c5b274b04031597b0",
        "--request-count",
        "64",
        "--input-tokens",
        "256",
        "--output-tokens",
        "128",
        "--concurrency",
        "16",
    )
    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"
    assert "fixed_text.py" in bundle.benchmark_command[1]
    assert bundle.benchmark_command[-2:] == ("--concurrency", "16")


def test_objectives_match_the_rf_protocol_metrics() -> None:
    objectives = (task_root(_BUNDLE_ROOT) / "objectives.toml").read_text(encoding="utf-8")

    assert 'name = "output_token_throughput_per_s"' in objectives
    assert 'name = "p90_latency_ms"' in objectives


def test_cpu_smoke_profile_declares_the_expected_request_and_result_contract() -> None:
    profile = load_profile(task_root(_BUNDLE_ROOT) / "benchmark" / "cpu_smoke.toml")

    assert profile.benchmark_path == _BENCHMARK
    assert profile.shape_counts == {(16, 4): 8}
    assert set(profile.metrics) == {"output_token_throughput_per_s", "p90_latency_ms"}
    assert profile.required_fields["stream_options"] == {"include_usage": True}
    assert {failure.mode for failure in profile.failures} == {
        "http",
        "malformed-sse",
        "truncated-sse",
        "output-mismatch",
    }
