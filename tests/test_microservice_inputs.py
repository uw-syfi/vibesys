from pathlib import Path

import pytest

from vibesys.input_manifest import load_input_bundle

PROJECT_ROOT = Path(__file__).parents[1]
MICROSERVICE_ROOT = PROJECT_ROOT / "examples" / "microservices"
MICROSERVICE_SCENARIOS = tuple(
    sorted(manifest.parent for manifest in MICROSERVICE_ROOT.glob("*/vibesys.input.toml"))
)


@pytest.mark.parametrize(
    "scenario_path",
    MICROSERVICE_SCENARIOS,
    ids=lambda path: path.name,
)
def test_microservice_scenario_uses_shared_evaluator(scenario_path: Path) -> None:
    bundle = load_input_bundle(
        scenario_path,
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


def test_microservice_scenarios_are_discovered() -> None:
    assert {path.name for path in MICROSERVICE_SCENARIOS} == {
        "social-network-read-timeline",
        "train-ticket",
    }


@pytest.mark.parametrize(
    "scenario_path",
    MICROSERVICE_SCENARIOS,
    ids=lambda path: path.name,
)
def test_microservice_scenario_has_no_embedded_legacy_generator(
    scenario_path: Path,
) -> None:
    benchmark_dir = scenario_path / "benchmark"
    legacy_sources = sorted(
        path.relative_to(scenario_path)
        for path in benchmark_dir.iterdir()
        if path.name == "benchmark" or path.suffix in {".cpp", ".py"}
    )

    assert legacy_sources == []
