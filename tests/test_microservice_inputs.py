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
    assert ("--fixture-seed", "random") in zip(
        bundle.benchmark_command,
        bundle.benchmark_command[1:],
        strict=False,
    )
    if scenario_path.name == "train-ticket":
        assert ("--seed", "random") in zip(
            bundle.benchmark_command,
            bundle.benchmark_command[1:],
            strict=False,
        )


def test_microservice_scenarios_are_discovered() -> None:
    assert {path.name for path in MICROSERVICE_SCENARIOS} == {
        "hotel-reservation",
        "social-network-read-timeline",
        "train-ticket",
    }


def test_train_ticket_accuracy_uses_shared_evaluator() -> None:
    bundle = load_input_bundle(
        MICROSERVICE_ROOT / "train-ticket",
        project_root=PROJECT_ROOT,
    )

    assert bundle.accuracy_command[:5] == (
        "go",
        "-C",
        "_evaluator/microservice",
        "run",
        "./cmd/servicebench",
    )
    assert bundle.accuracy_command[5:7] == ("--mode", "accuracy")


def test_hotel_accuracy_uses_shared_evaluator_with_random_cases() -> None:
    bundle = load_input_bundle(
        MICROSERVICE_ROOT / "hotel-reservation",
        project_root=PROJECT_ROOT,
    )

    assert bundle.accuracy_command[:7] == (
        "go",
        "-C",
        "_evaluator/microservice",
        "run",
        "./cmd/servicebench",
        "--mode",
        "accuracy",
    )
    assert ("--seed", "random") in zip(
        bundle.accuracy_command,
        bundle.accuracy_command[1:],
        strict=False,
    )
    assert ("--seed", "random") in zip(
        bundle.benchmark_command,
        bundle.benchmark_command[1:],
        strict=False,
    )
    assert ("--fixture-seed", "random") in zip(
        bundle.benchmark_command,
        bundle.benchmark_command[1:],
        strict=False,
    )
    assert ("--candidate-dir", "../../hotelReservation") in zip(
        bundle.accuracy_command,
        bundle.accuracy_command[1:],
        strict=False,
    )
    assert (
        "--run-command-json",
        '["docker","compose","up","-d","--build"]',
    ) in zip(bundle.accuracy_command, bundle.accuracy_command[1:], strict=False)
    assert (
        "--stop-command-json",
        '["docker","compose","stop","-t","10","frontend","geo","profile","rate",'
        '"recommendation","reservation","search","user"]',
    ) in zip(bundle.accuracy_command, bundle.accuracy_command[1:], strict=False)
    assert (
        "--cleanup-command-json",
        '["docker","compose","down","-v","--remove-orphans"]',
    ) in zip(bundle.accuracy_command, bundle.accuracy_command[1:], strict=False)
    assert ("--candidate-dir", "../../hotelReservation") in zip(
        bundle.benchmark_command,
        bundle.benchmark_command[1:],
        strict=False,
    )
    assert (
        "--run-command-json",
        '["docker","compose","up","-d","--build"]',
    ) in zip(bundle.benchmark_command, bundle.benchmark_command[1:], strict=False)
    assert (
        "--stop-command-json",
        '["docker","compose","stop","-t","10","frontend","geo","profile","rate",'
        '"recommendation","reservation","search","user"]',
    ) in zip(bundle.benchmark_command, bundle.benchmark_command[1:], strict=False)
    assert (
        "--cleanup-command-json",
        '["docker","compose","down","-v","--remove-orphans"]',
    ) in zip(bundle.benchmark_command, bundle.benchmark_command[1:], strict=False)


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


def test_hotel_workload_preserves_canonical_mix_and_stateful_gate() -> None:
    workload_path = MICROSERVICE_ROOT / "hotel-reservation" / "benchmark" / "workload.toml"
    with workload_path.open("rb") as file:
        workload = tomllib.load(file)

    operations = {operation["name"]: operation for operation in workload["operations"]}
    assert {name: operation["weight"] for name, operation in operations.items()} == {
        "search_hotels": 600,
        "recommend_distance": 130,
        "recommend_rate": 130,
        "recommend_price": 130,
        "login_valid": 3,
        "login_invalid": 2,
        "reserve_capacity": 5,
    }
    assert operations["reserve_capacity"]["tags"] == ["write", "read-your-write"]
    assert workload["load"]["model"] == "closed_loop"
    assert workload["load"]["repetitions"] == 3
    assert workload["profiles"]["quick"]["repetitions"] == 1
    assert workload["constraints"] == {
        "min_success_rate": 1.0,
        "max_error_rate": 0.0,
        "min_operations_per_type": 1,
    }
