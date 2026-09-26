"""Pure persisted-state contracts for evolutionary-search populations."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    FiniteFloat,
    field_validator,
    model_validator,
)

from vibesys.loops.state._codec import (
    JsonObject,
    invalid_state,
    parse_json_object,
    serialize_json_object,
)

PositiveId = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonEmptyText = Annotated[str, Field(min_length=1)]


class _StrictStateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class IndividualRecord(_StrictStateModel):
    """One durable evolutionary candidate and its lineage and fitness data."""

    id: PositiveId
    generation: NonNegativeInt
    parent_id: PositiveId | None
    inspiration_ids: tuple[PositiveId, ...] = ()
    commit: NonEmptyText | None = None
    perf_metric: FiniteFloat | None = None
    perf_unit: NonEmptyText | None = None
    metrics: dict[str, FiniteFloat] = Field(default_factory=dict)
    passed: bool = False
    summary: str = ""
    feedback: str = ""
    policy_parent_id: NonEmptyText | None = None
    policy_target_island: NonNegativeInt | None = None

    @field_validator("commit", "perf_unit", "policy_parent_id")
    @classmethod
    def _reject_blank_optional_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            invalid_state("optional identifiers must not be blank")
        return value

    @field_validator("metrics")
    @classmethod
    def _validate_metric_names(cls, metrics: dict[str, float]) -> dict[str, float]:
        if any(not name.strip() for name in metrics):
            invalid_state("metric names must not be empty")
        return metrics

    @model_validator(mode="after")
    def _validate_record(self) -> Self:
        if self.parent_id == self.id:
            invalid_state("parent_id cannot reference the individual itself")
        if len(self.inspiration_ids) != len(set(self.inspiration_ids)):
            invalid_state("inspiration_ids must be unique")
        if self.id in self.inspiration_ids:
            invalid_state("inspiration_ids cannot reference the individual itself")
        if self.parent_id is not None and self.parent_id in self.inspiration_ids:
            invalid_state("the parent cannot also be an inspiration")
        if self.passed and self.commit is None:
            invalid_state("a passing individual requires a commit")
        if self.perf_metric is None and self.perf_unit is not None:
            invalid_state("perf_unit requires perf_metric")
        return self


class PopulationSnapshot(_StrictStateModel):
    """Versioned, referentially valid archive of evolutionary candidates."""

    version: Literal[1] = 1
    individuals: tuple[IndividualRecord, ...] = ()

    @model_validator(mode="after")
    def _validate_population(self) -> Self:
        ids = [individual.id for individual in self.individuals]
        if len(ids) != len(set(ids)):
            invalid_state("individual ids must be unique")
        if ids != sorted(ids):
            invalid_state("individuals must be ordered by increasing id")

        by_id = {individual.id: individual for individual in self.individuals}
        for individual in self.individuals:
            references = (
                () if individual.parent_id is None else (individual.parent_id,)
            ) + individual.inspiration_ids
            for reference_id in references:
                referenced = by_id.get(reference_id)
                if referenced is None:
                    invalid_state(
                        f"individual {individual.id} references missing individual {reference_id}"
                    )
                if reference_id >= individual.id:
                    invalid_state(
                        f"individual {individual.id} must reference an earlier individual id"
                    )
                if referenced.generation > individual.generation:
                    invalid_state(f"individual {individual.id} references a later generation")
                if not referenced.passed:
                    invalid_state(
                        f"individual {individual.id} references failed individual {reference_id}"
                    )
        return self


def serialize_population_snapshot(snapshot: PopulationSnapshot) -> JsonObject:
    """Return the stable JSON-compatible representation of *snapshot*."""
    return serialize_json_object(snapshot)


def parse_population_snapshot(data: JsonObject) -> PopulationSnapshot:
    """Parse a population snapshot from its JSON-compatible representation."""
    return parse_json_object(PopulationSnapshot, data)
