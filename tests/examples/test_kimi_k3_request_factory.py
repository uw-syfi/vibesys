from pathlib import Path

from tests.examples.example_task import load_example_task, task_root
from tests.examples.request_factory_cpu_smoke import load_profile

_REPO_ROOT = Path(__file__).parents[2]
_BUNDLE_ROOT = _REPO_ROOT / "examples" / "model-serving" / "kimi-k3-8xb200"


def test_bundle_uses_request_factory_adapter_and_result_protocol_v2() -> None:
    bundle = load_example_task(_BUNDLE_ROOT)

    assert bundle.manifest.evaluator is not None
    assert bundle.manifest.evaluator.name == "vibesys-evaluator-request-factory"
    assert bundle.manifest.benchmark.entrypoint == "request-factory-adapter"
    assert bundle.manifest.benchmark.args == (
        "${PROJECT_ROOT}/.vibesys/tasks/default/benchmark/benchmark.py",
    )
    assert bundle.benchmark_result_protocol == 2
    assert bundle.benchmark_output_argument == "--vs-output"


def test_objectives_match_the_metrics_declared_by_the_adapter() -> None:
    objectives = (task_root(_BUNDLE_ROOT) / "objectives.toml").read_text(encoding="utf-8")

    assert 'name = "output_token_throughput_per_s"' in objectives
    assert 'name = "p90_latency_ms"' in objectives


def test_cpu_smoke_profile_declares_the_expected_request_and_result_contract() -> None:
    profile = load_profile(task_root(_BUNDLE_ROOT) / "benchmark" / "cpu_smoke.toml")

    assert profile.benchmark_path == task_root(_BUNDLE_ROOT) / "benchmark" / "benchmark.py"
    assert profile.shape_counts == {(16, 4): 8}
    assert set(profile.metrics) == {"output_token_throughput_per_s", "p90_latency_ms"}
    assert profile.required_fields["stream_options"] == {"include_usage": True}
