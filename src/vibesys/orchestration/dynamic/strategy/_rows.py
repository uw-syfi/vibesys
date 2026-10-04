"""Immutable scientific readings shared by state, prompt contexts and operation outcomes."""

from typing import Annotated, Literal

from pydantic import Field

from vs_core.api import EvidenceId, EvidenceKind, Value

type Finite = Annotated[float, Field(allow_inf_nan=False)]
type Direction = Literal["max", "min"]


class MetricRow(Value):
    """One measured quantity on a declared axis."""

    name: str = Field(min_length=1)
    value: Finite
    direction: Direction
    unit: str | None = None


class PartialRow(Value):
    """What a failed benchmark measured before it stopped, as its evaluator reported it."""

    name: str = Field(min_length=1)
    value: Finite
    direction: Direction
    unit: str | None = None
    target: Finite | None = None
    completed: int | None = Field(default=None, ge=0)
    required: int | None = Field(default=None, gt=0)
    progress_unit: str | None = None


class EvidenceReading(Value):
    """Typed reading of one core `EvidenceRef`, decoded by the evidence owner.

    Core evidence carries identity and artifacts but no numbers; the numeric facts
    arrive through the declared `InterpretEvidence` operation, never by parsing
    artifacts inside the strategy.
    """

    evidence_id: EvidenceId
    kind: EvidenceKind
    passed: bool
    stage: str = Field(min_length=1)
    protocol: int = Field(default=1, ge=1)
    metrics: tuple[MetricRow, ...] = ()
    partial: PartialRow | None = None
    feedback: str = ""

    def headline(self) -> MetricRow | None:
        """The first reported metric, the benchmark's headline reading."""
        return self.metrics[0] if self.metrics else None


def reading_of(readings: tuple[EvidenceReading, ...], kind: EvidenceKind) -> EvidenceReading | None:
    """The latest decoded reading of one evidence kind, or None when absent."""
    matching = [item for item in readings if item.kind is kind]
    return matching[-1] if matching else None
