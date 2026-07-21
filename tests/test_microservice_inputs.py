import tomllib
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
        "./cmd/servicebench",
    )
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.json_argument == "--output-json"
    assert bundle.benchmark_result.metric == "primary_value"
    if scenario_path.name == "social-network-read-timeline":
        assert bundle.benchmark_command[-2:] == ("--fixture-seed", "random")
    else:
        assert bundle.benchmark_command[-2:] == ("--seed", "random")


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


def test_social_network_workload_uses_stateful_semantic_operation() -> None:
    workload_path = (
        MICROSERVICE_ROOT / "social-network-read-timeline" / "benchmark" / "workload.toml"
    )
    with workload_path.open("rb") as file:
        workload = tomllib.load(file)

    operations = {operation["name"]: operation for operation in workload["operations"]}
    assert set(operations) == {
        "user_timeline_read",
        "home_timeline_read",
        "compose_user_timeline",
    }
    assert operations["compose_user_timeline"]["tags"] == [
        "write",
        "read-your-write",
    ]
    assert {
        capture["header"] for capture in operations["compose_user_timeline"]["capture_headers"]
    } == {
        "X-Compose-Thrift-Ms",
        "X-UserTimeline-Thrift-Ms",
        "X-HomeTimeline-Thrift-Ms",
    }
    assert workload["load"]["seed"] == 42
    assert workload["load"]["fixture_seed"] == 42
    assert workload["constraints"]["min_operations_per_type"] == 1
