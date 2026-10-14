"""Versioned architecture-graph contract published to clients.

The topology is stable across campaign measurements.  Observations hang from
recorded measurement identities and carry full attribution snapshots, so a
client can scrub history without reconstructing backend facts.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal, NoReturn, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$"),
]
Label = Annotated[str, Field(min_length=1, max_length=160)]
Description = Annotated[str, Field(min_length=1, max_length=2_000)]


class ArchitectureGenerationApproach(StrEnum):
    """How the producer assembled topology and attribution facts."""

    STATIC_ANALYSIS = "static-analysis"
    PROFILER_DERIVED = "profiler-derived"
    AGENT_AUTHORED = "agent-authored"
    HYBRID = "hybrid"


class ArchitectureComponentKind(StrEnum):
    """Closed presentation-neutral component categories."""

    API = "api"
    DATABASE = "database"
    EXTERNAL = "external"
    LIBRARY = "library"
    PROCESS = "process"
    QUEUE = "queue"
    SERVICE = "service"
    STORAGE = "storage"


class ArchitectureAnchorKind(StrEnum):
    """Stable namespace that gives a component its cross-measurement identity."""

    MODULE = "module"
    PATH = "path"
    SERVICE = "service"


class ArchitectureEdgeKind(StrEnum):
    """Semantic relationship rendered independently of its geometry."""

    CALL = "call"
    DATA = "data"
    DEPENDENCY = "dependency"
    OWNERSHIP = "ownership"
    QUEUE = "queue"
    STORAGE = "storage"


class _ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ArchitectureGeneration(_ContractModel):
    """Provenance for one graph document."""

    approach: ArchitectureGenerationApproach
    source: Description


class ArchitectureAnchor(_ContractModel):
    """A stable path, module, or service identity; never a layout coordinate."""

    kind: ArchitectureAnchorKind
    value: Annotated[str, Field(min_length=1, max_length=512)]


class ArchitectureComponent(_ContractModel):
    """One stable node in the system topology."""

    id: Identifier
    label: Label
    kind: ArchitectureComponentKind
    anchor: ArchitectureAnchor
    description: Description
    group: Label | None


class ArchitectureEdge(_ContractModel):
    """One stable directed relationship between components."""

    id: Identifier
    source_id: Identifier
    target_id: Identifier
    kind: ArchitectureEdgeKind
    label: Label | None


class ArchitectureAttributionValue(_ContractModel):
    """A non-negative profiler value with explicit units and provenance."""

    value: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    unit: Annotated[str, Field(min_length=1, max_length=32)]
    provenance: Description


class ArchitectureComponentAttribution(_ContractModel):
    """Optional cost and latency slots for one component at one observation."""

    component_id: Identifier
    cost: ArchitectureAttributionValue | None
    latency: ArchitectureAttributionValue | None


class ArchitectureComponentChange(_ContractModel):
    """Recorded files that changed one stable component at an observation."""

    component_id: Identifier
    changed_files: Annotated[tuple[str, ...], Field(min_length=1, max_length=2_000)]

    @field_validator("changed_files")
    @classmethod
    def _changed_files_are_normalized(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            path = PurePosixPath(value)
            if (
                not value
                or value == "."
                or value.startswith("/")
                or "\\" in value
                or path.as_posix() != value
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                _fail_validation(f"changed file must be a normalized relative path: {value!r}")
        if len(set(values)) != len(values):
            _fail_validation("changed_files contains a duplicate path")
        return values


class ArchitectureObservation(_ContractModel):
    """Architecture facts aligned to one campaign measurement cursor."""

    measurement_id: Identifier
    sequence: Annotated[int, Field(ge=0)]
    label: Label
    changes: Annotated[tuple[ArchitectureComponentChange, ...], Field(max_length=500)]
    attributions: Annotated[tuple[ArchitectureComponentAttribution, ...], Field(max_length=500)]


class ArchitectureGraphDocument(_ContractModel):
    """The complete version-1 architecture graph and its campaign observations."""

    schema_version: Literal[1]
    graph_id: Identifier
    title: Label
    description: Description
    generation: ArchitectureGeneration
    components: Annotated[tuple[ArchitectureComponent, ...], Field(min_length=1, max_length=500)]
    edges: Annotated[tuple[ArchitectureEdge, ...], Field(max_length=2_000)]
    observations: Annotated[tuple[ArchitectureObservation, ...], Field(max_length=2_000)]

    @model_validator(mode="after")
    def _references_are_closed_and_ordered(self) -> Self:
        component_ids = _unique_ids(self.components, "components")
        edge_ids = _unique_ids(self.edges, "edges")
        del edge_ids
        observation_ids = _unique_ids(self.observations, "observations", key="measurement_id")
        del observation_ids

        for index, edge in enumerate(self.edges):
            _require_reference(edge.source_id, component_ids, f"edges[{index}].source_id")
            _require_reference(edge.target_id, component_ids, f"edges[{index}].target_id")
            if edge.source_id == edge.target_id:
                _fail_validation(f"edges[{index}] is a self-edge")

        previous_sequence = -1
        for index, observation in enumerate(self.observations):
            if observation.sequence <= previous_sequence:
                _fail_validation(
                    f"observations[{index}].sequence must be strictly greater than "
                    f"{previous_sequence}"
                )
            previous_sequence = observation.sequence
            change_ids = _unique_ids(
                observation.changes,
                f"observations[{index}].changes",
                key="component_id",
            )
            for component_id in change_ids:
                _require_reference(
                    component_id,
                    component_ids,
                    f"observations[{index}].changes",
                )
            attribution_ids = _unique_ids(
                observation.attributions,
                f"observations[{index}].attributions",
                key="component_id",
            )
            for component_id in attribution_ids:
                _require_reference(
                    component_id,
                    component_ids,
                    f"observations[{index}].attributions",
                )
        return self


def _unique_ids(values: tuple[object, ...], path: str, *, key: str = "id") -> set[str]:
    result: set[str] = set()
    for index, value in enumerate(values):
        identifier = getattr(value, key)
        if identifier in result:
            _fail_validation(f"{path}[{index}].{key} duplicates {identifier!r}")
        result.add(identifier)
    return result


def _require_reference(value: str, known: set[str], path: str) -> None:
    if value not in known:
        _fail_validation(f"{path} references unknown component {value!r}")


def _fail_validation(message: str) -> NoReturn:
    raise ValueError(message)


__all__ = [
    "ArchitectureAnchor",
    "ArchitectureAnchorKind",
    "ArchitectureAttributionValue",
    "ArchitectureComponent",
    "ArchitectureComponentAttribution",
    "ArchitectureComponentChange",
    "ArchitectureComponentKind",
    "ArchitectureEdge",
    "ArchitectureEdgeKind",
    "ArchitectureGeneration",
    "ArchitectureGenerationApproach",
    "ArchitectureGraphDocument",
    "ArchitectureObservation",
]
