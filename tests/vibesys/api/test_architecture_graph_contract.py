"""Public contract and committed-fixture coverage for architecture graphs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from scripts.generate_architecture_graph_contract import (
    architecture_graph_json_schema,
    schema_typescript,
)

from vibesys.api.architecture import ArchitectureGraphDocument

if TYPE_CHECKING:
    from collections.abc import Callable

FIXTURE_ROOT = Path("clients/features/campaign/src/fixtures")
SCHEMA_PATH = Path("clients/libs/contracts/src/generated/architecture-graph.schema.json")
SCHEMA_MODULE_PATH = Path("clients/libs/contracts/src/generated/architecture-graph.schema.ts")
FIXTURE_PATHS = (
    FIXTURE_ROOT / "architecture-queue.json",
    FIXTURE_ROOT / "architecture-microservices.json",
)


def _fixture(path: Path = FIXTURE_PATHS[0]) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.mark.parametrize("path", FIXTURE_PATHS)
def test_realistic_fixture_is_an_authoritative_model_dump(path: Path) -> None:
    graph = ArchitectureGraphDocument.model_validate_json(path.read_text(encoding="utf-8"))

    assert graph.schema_version == 1
    assert graph.components
    assert all(observation.measurement_id for observation in graph.observations)
    assert graph.model_dump(mode="json") == _fixture(path)


def test_queue_and_microservice_fixtures_exercise_different_attribution_absence() -> None:
    queue = ArchitectureGraphDocument.model_validate_json(
        FIXTURE_PATHS[0].read_text(encoding="utf-8")
    )
    microservices = ArchitectureGraphDocument.model_validate_json(
        FIXTURE_PATHS[1].read_text(encoding="utf-8")
    )

    assert {component.kind.value for component in queue.components} >= {"api", "queue", "storage"}
    assert {component.kind.value for component in microservices.components} >= {
        "api",
        "service",
        "database",
    }
    values = [
        attribution
        for graph in (queue, microservices)
        for observation in graph.observations
        for attribution in observation.attributions
    ]
    assert any(value.cost is None for value in values)
    assert any(value.latency is None for value in values)
    assert any(value.cost is not None and value.latency is not None for value in values)


@given(order=st.permutations(tuple(range(6))))
def test_component_input_order_does_not_change_contract_identity(order: list[int]) -> None:
    value = _fixture()
    components = value["components"]
    assert isinstance(components, list)
    value["components"] = [components[index] for index in order]

    graph = ArchitectureGraphDocument.model_validate_json(json.dumps(value))

    assert {component.id for component in graph.components} == {
        "queue.ffi",
        "queue.owner",
        "queue.producer",
        "queue.mutex",
        "queue.deque",
        "queue.consumer",
    }


def _add_unknown_component_field(value: dict[str, Any]) -> None:
    value["components"][0]["shadow"] = True


def _add_dangling_edge(value: dict[str, Any]) -> None:
    value["edges"][0]["target_id"] = "missing"


def _add_dangling_change(value: dict[str, Any]) -> None:
    value["observations"][1]["changes"][0]["component_id"] = "missing"


def _add_parent_changed_path(value: dict[str, Any]) -> None:
    value["observations"][1]["changes"][0]["changed_files"].append("../outside")


def _add_current_directory_changed_path(value: dict[str, Any]) -> None:
    value["observations"][1]["changes"][0]["changed_files"].append(".")


def _reverse_observation_order(value: dict[str, Any]) -> None:
    value["observations"][1]["sequence"] = 1


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_add_unknown_component_field, "components.0.shadow"),
        (_add_dangling_edge, "references unknown component 'missing'"),
        (_add_dangling_change, "references unknown component 'missing'"),
        (_add_parent_changed_path, "normalized relative path"),
        (_add_current_directory_changed_path, "normalized relative path"),
        (_reverse_observation_order, "must be strictly greater"),
    ],
)
def test_malformed_graphs_fail_at_the_public_boundary(
    mutate: Callable[[dict[str, Any]], None], message: str
) -> None:
    value = _fixture()
    mutate(value)

    with pytest.raises(ValidationError, match=message):
        ArchitectureGraphDocument.model_validate_json(json.dumps(value))


def test_committed_schema_artifacts_match_the_python_contract() -> None:
    assert json.loads(SCHEMA_PATH.read_text(encoding="utf-8")) == architecture_graph_json_schema()
    assert SCHEMA_MODULE_PATH.read_text(encoding="utf-8") == schema_typescript()
