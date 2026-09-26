"""Pure persisted-state contracts for the issue-driven plain loop."""

from __future__ import annotations

import math
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
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
PlainPhase = Literal["implementer", "judge", "perf_eval"]
PerformanceTrend = Literal["improved", "regressed", "mixed"]


class _StrictStateModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, validate_assignment=True)


def _reject_non_finite_numbers(value: JsonValue, *, path: str = "metrics") -> None:
    if isinstance(value, float) and not math.isfinite(value):
        invalid_state(f"{path} must contain only finite numbers")
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_non_finite_numbers(item, path=f"{path}[{index}]")
    elif isinstance(value, dict):
        for key, item in value.items():
            _reject_non_finite_numbers(item, path=f"{path}[{key!r}]")


class PlainLoopCursor(_StrictStateModel):
    """Portable cursor required to resume the plain loop."""

    version: Literal[1] = 1
    round_idx: NonNegativeInt = 0
    phase: PlainPhase = "implementer"
    current_issue_id: PositiveId | None = None
    bootstrap_done: bool = False

    @model_validator(mode="after")
    def _validate_phase_issue(self) -> Self:
        if self.phase == "judge" and self.current_issue_id is None:
            invalid_state("judge phase requires current_issue_id")
        if self.phase == "perf_eval" and self.current_issue_id is not None:
            invalid_state("perf_eval phase cannot reference a current issue")
        return self

    def transition(
        self,
        *,
        round_idx: int,
        phase: PlainPhase,
        current_issue_id: int | None,
    ) -> Self:
        """Return one atomically validated cursor transition."""
        return type(self).model_validate(
            {
                **self.model_dump(mode="python"),
                "round_idx": round_idx,
                "phase": phase,
                "current_issue_id": current_issue_id,
            },
            strict=True,
        )


class PlainPerformanceRecord(_StrictStateModel):
    """One typed performance-evaluation entry in plain-loop history."""

    iteration: PositiveId
    timestamp: AwareDatetime
    throughput_trend: PerformanceTrend
    latency_trend: PerformanceTrend
    metrics: dict[str, JsonValue]
    new_issue_ids: tuple[PositiveId, ...] = ()

    @field_validator("metrics")
    @classmethod
    def _validate_metrics(cls, metrics: dict[str, JsonValue]) -> dict[str, JsonValue]:
        for key in metrics:
            if not key.strip():
                invalid_state("metric names must not be empty")
        _reject_non_finite_numbers(metrics)
        return metrics

    @field_validator("new_issue_ids")
    @classmethod
    def _validate_new_issue_ids(cls, issue_ids: tuple[int, ...]) -> tuple[int, ...]:
        if len(issue_ids) != len(set(issue_ids)):
            invalid_state("new_issue_ids must be unique")
        return issue_ids


class PlainPerformanceSnapshot(_StrictStateModel):
    """Versioned portable performance history for one plain-loop run."""

    version: Literal[1] = 1
    records: tuple[PlainPerformanceRecord, ...] = ()

    @model_validator(mode="after")
    def _validate_history_order(self) -> Self:
        iterations = [record.iteration for record in self.records]
        if iterations != sorted(iterations) or len(iterations) != len(set(iterations)):
            invalid_state("performance record iterations must be strictly increasing")
        return self


def serialize_plain_loop_cursor(cursor: PlainLoopCursor) -> JsonObject:
    """Return the stable JSON-compatible representation of *cursor*."""
    return serialize_json_object(cursor)


def parse_plain_loop_cursor(data: JsonObject) -> PlainLoopCursor:
    """Parse a plain-loop cursor from its JSON-compatible representation."""
    return parse_json_object(PlainLoopCursor, data)


def serialize_plain_performance_snapshot(snapshot: PlainPerformanceSnapshot) -> JsonObject:
    """Return the stable JSON-compatible representation of *snapshot*."""
    return serialize_json_object(snapshot)


def parse_plain_performance_snapshot(data: JsonObject) -> PlainPerformanceSnapshot:
    """Parse plain-loop performance history from a JSON-compatible object."""
    return parse_json_object(PlainPerformanceSnapshot, data)
