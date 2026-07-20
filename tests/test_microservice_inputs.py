from pathlib import Path

import pytest

from vibesys.input_manifest import load_input_bundle

PROJECT_ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "scenario",
    ["train-ticket", "social-network-read-timeline"],
)
def test_microservice_scenario_uses_shared_evaluator(scenario: str) -> None:
    bundle = load_input_bundle(
        PROJECT_ROOT / "examples" / "microservices" / scenario,
        project_root=PROJECT_ROOT,
    )

    assert bundle.evaluator_path == PROJECT_ROOT / "examples" / "evaluators" / "microservice"
    assert bundle.benchmark_command[:5] == (
        "go",
        "-C",
        "_evaluator/microservice",
        "run",
        "./cmd/microbench",
    )
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.json_argument == "--output-json"
    assert bundle.benchmark_result.metric == "primary_value"
