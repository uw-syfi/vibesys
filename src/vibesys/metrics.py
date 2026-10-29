"""Objective axes and metric comparison owned by orchestration policy.

A run measures its candidates in a **metric space**: a set of directed
objective axes plus the relative benchmark tolerance below which two readings
are indistinguishable. :class:`MetricSpace` owns every question that needs both
of those facts -- "is this reading better?", "does this row dominate that
one?", "which rows are on the frontier?", "which reading is best?" -- so that
noise and direction appear as parameters here and nowhere else. Callers hand it
measurements and consume a :class:`MetricComparison`; they do not assemble a
comparison out of a value, a direction, and a tolerance.

The space is built once from the task's ``objectives.toml`` and persisted with
the run, so the loop, ``--resume`` reprojection, and the server read path all
compare within the same space instead of being handed a tolerance through a
call chain (issue #507).

``MetricComparison`` is persisted by hypothesis history and owned here with
the comparison policy that produces it.
"""

from __future__ import annotations

from dataclasses import dataclass as standard_dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator
from pydantic.dataclasses import dataclass

from vs_core.api import (
    EvidenceId,
    EvidenceKey,
    EvidenceKind,
    EvidenceRef,
    ObservationStatus,
    RequestId,
    RunView,
    Scope,
    Value,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


__all__ = [
    "AcceptedReading",
    "EvidenceReading",
    "FrameworkBenchmarkOutcome",
    "Measurement",
    "MetricComparison",
    "MetricRow",
    "MetricSpace",
    "Objective",
    "PartialRow",
    "accept_readings",
    "judged_evidence",
    "reading_of",
]


class MetricComparison(StrEnum):
    """How one measurement relates to the measurement it is compared against.

    ``WITHIN_NOISE`` and ``INCOMPARABLE`` are deliberately distinct.
    ``WITHIN_NOISE`` means both readings exist on the same axis and their
    difference does not exceed the declared measurement tolerance, so the
    comparison ran and found no result. ``INCOMPARABLE`` means the comparison
    could not run at all: a reading is missing, the axis has no known
    direction, or the runs give no estimate of the noise. Consumers that collapse them still have to say so explicitly.
    """

    BETTER = "better"
    WORSE = "worse"
    WITHIN_NOISE = "within_noise"
    INCOMPARABLE = "incomparable"


@standard_dataclass(frozen=True, slots=True)
class FrameworkBenchmarkOutcome:
    """Policy-interpreted benchmark headline and full measured row."""

    feedback: str | None = None
    metric_name: str | None = None
    metric_value: float | None = None
    metric_direction: Literal["max", "min"] | None = None
    metric_unit: str | None = None
    row: Mapping[str, float] | None = None


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class Objective:
    """One directed axis of the fitness frontier.

    ``name`` is the key in a metric row the profiler must report.
    ``direction`` is ``"max"`` or ``"min"``; the framework flips the sign when
    comparing min-objectives so dominance logic can treat every axis as
    "higher is better" internally.
    """

    name: str
    direction: Literal["max", "min"]

    def signed(self, value: float) -> float:
        """Return *value* flipped to "higher is better" semantics.

        Used by the dominance helper so callers don't have to branch on
        direction. Min objectives are negated; max objectives pass through.
        """
        return value if self.direction == "max" else -value


@dataclass(frozen=True, config=ConfigDict(extra="forbid"))
class Measurement:
    """One benchmark reading, named by the axis it was taken on.

    ``direction`` is data about the reading, not a comparison parameter: a
    record written before its axis was declared in ``objectives.toml`` carries
    the direction the framework used at the time. When it is absent the space's
    configured axis supplies it.
    """

    metric: str
    value: float
    direction: Literal["max", "min"] | None = None


class MetricSpace(BaseModel):
    """The directed axes and measurement tolerance one run compares within.

    ``relative_noise`` is the run's declared benchmark variation, read from
    ``[pareto] relative_noise``. A reading is better than a baseline only when
    its direction-signed improvement exceeds ``abs(baseline) * relative_noise``;
    symmetrically for worse. A zero fraction therefore reproduces strict
    comparison, and so does a zero baseline, for which a relative tolerance has
    no scale.

    The default is the empty strict space. It is the compatibility value for
    run state written before the space was persisted, and the only sanctioned
    way to get a zero tolerance: no function anywhere takes a tolerance
    argument that could silently default to it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    objectives: tuple[Objective, ...] = ()
    relative_noise: Annotated[FiniteFloat, Field(ge=0.0, lt=1.0)] = 0.0

    @model_validator(mode="after")
    def _unique_axes(self) -> MetricSpace:
        names = [objective.name for objective in self.objectives]
        if len(set(names)) != len(names):
            message = "objective names must be unique"
            raise ValueError(message)
        return self

    # -- axes ---------------------------------------------------------------

    @property
    def primary(self) -> Objective | None:
        """Return the axis that names the run's headline metric, if any."""
        return self.objectives[0] if self.objectives else None

    def axis(self, metric: str | None) -> Objective | None:
        """Return the configured axis named *metric*, if the space has one."""
        if metric is None:
            return None
        return next((item for item in self.objectives if item.name == metric), None)

    def signed_primary(self, value: float) -> float:
        """Orient a headline value so larger ranks better in this space.

        The empty compatibility space keeps the original scalar maximization
        semantics. A configured space applies its primary axis direction;
        callers use this only for values representing that headline axis.
        """
        primary = self.primary
        return value if primary is None else primary.signed(value)

    def direction(self, measurement: Measurement | None) -> Literal["max", "min"] | None:
        """Resolve which way is better for *measurement*.

        The reading's own direction wins, so a record measured before its axis
        was configured keeps the meaning it was written with; otherwise the
        configured axis decides.
        """
        if measurement is None:
            return None
        if measurement.direction is not None:
            return measurement.direction
        axis = self.axis(measurement.metric)
        return axis.direction if axis is not None else None

    # -- comparison ---------------------------------------------------------

    def compare(
        self,
        candidate: Measurement | None,
        baseline: Measurement | None,
    ) -> MetricComparison:
        """Order *candidate* against *baseline* within this space.

        A missing reading, mismatched axes, or an axis with no known direction
        yields ``INCOMPARABLE``: the framework must not read a verdict out of
        evidence it does not have.
        """
        direction = self.direction(candidate)
        if (
            candidate is None
            or baseline is None
            or direction is None
            or candidate.metric != baseline.metric
        ):
            return MetricComparison.INCOMPARABLE
        improvement = (
            candidate.value - baseline.value
            if direction == "max"
            else baseline.value - candidate.value
        )
        tolerance = abs(baseline.value) * self.relative_noise
        if improvement > tolerance:
            return MetricComparison.BETTER
        if improvement < -tolerance:
            return MetricComparison.WORSE
        return MetricComparison.WITHIN_NOISE

    def best[T](
        self,
        points: Sequence[T],
        reading: Callable[[T], Measurement | None],
    ) -> T | None:
        """Return the member of *points* whose reading leads on its axis.

        Ties, including ties within the tolerance, keep the earlier member, so
        callers order *points* to express their own tie-break. Members with no
        reading, or on an axis with no known direction, are skipped.
        """
        best: T | None = None
        best_reading: Measurement | None = None
        for point in points:
            item = reading(point)
            if item is None or self.direction(item) is None:
                continue
            if best_reading is None or self.compare(item, best_reading) is MetricComparison.BETTER:
                best, best_reading = point, item
        return best

    def compare_to_best(
        self,
        candidate: Measurement | None,
        prior: Sequence[Measurement],
    ) -> MetricComparison:
        """Order *candidate* against the best of *prior* on the same axis.

        An empty history is ``BETTER``: the first trusted reading advances an
        archive that holds nothing. A candidate whose direction is unknown is
        ``INCOMPARABLE`` even then, because nothing about it can be ordered.
        """
        if candidate is None or self.direction(candidate) is None:
            return MetricComparison.INCOMPARABLE
        best = self.best(
            [item for item in prior if item.metric == candidate.metric],
            lambda item: item,
        )
        if best is None:
            return MetricComparison.BETTER
        return self.compare(candidate, best)

    # -- frontier -----------------------------------------------------------

    def dominates(self, a: Mapping[str, float], b: Mapping[str, float]) -> bool:
        """Return whether row *a* materially dominates row *b*.

        A row dominates only when it is no worse than *b* within the declared
        tolerance on every configured axis and better by more than that
        tolerance on at least one. A missing axis makes the rows incomparable.
        This intentionally retains near-equal alternatives instead of
        converting a sub-noise fluctuation into an automatic rollback.
        """
        if not self.objectives:
            return False
        materially_better = False
        for objective in self.objectives:
            comparison = self.compare(
                _reading(objective, a),
                _reading(objective, b),
            )
            match comparison:
                case MetricComparison.INCOMPARABLE | MetricComparison.WORSE:
                    return False
                case MetricComparison.BETTER:
                    materially_better = True
                case MetricComparison.WITHIN_NOISE:
                    pass
        return materially_better

    def frontier[T](
        self,
        points: Sequence[T],
        row: Callable[[T], Mapping[str, float]],
    ) -> list[T]:
        """Return the members of *points* that nothing else dominates."""
        return [
            point
            for point in points
            if not any(
                other is not point and self.dominates(row(other), row(point)) for other in points
            )
        ]

    def complete(self, row: Mapping[str, float]) -> bool:
        """Return whether *row* carries a value for every configured axis."""
        return bool(self.objectives) and all(objective.name in row for objective in self.objectives)


def _reading(objective: Objective, row: Mapping[str, float]) -> Measurement | None:
    value = row.get(objective.name)
    if value is None:
        return None
    return Measurement(metric=objective.name, value=value, direction=objective.direction)


# -- decoded readings of core evidence ------------------------------------------------------
#
# Core evidence carries identity and artifacts but no numbers. The numeric facts arrive through
# a declared interpretation operation, never by parsing artifacts inside a strategy, as typed
# readings. These are the immutable shapes every strategy keeps in its state and hands to
# prompt contexts, and the one rule that binds a reading to the record it decodes.

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
    """Typed reading of one core `EvidenceRef`, decoded by the evidence owner."""

    evidence_id: EvidenceId
    kind: EvidenceKind
    passed: bool
    stage: str = Field(min_length=1)
    protocol: int = Field(default=1, ge=1)
    metrics: tuple[MetricRow, ...] = ()
    partial: PartialRow | None = None
    feedback: str = ""
    unmet_requirement: str | None = Field(default=None, exclude_if=lambda value: value is None)
    """The capability a failed benchmark's evaluator named as unmet, when it named one."""

    def headline(self) -> MetricRow | None:
        """The first reported metric, the benchmark's headline reading."""
        return self.metrics[0] if self.metrics else None


class AcceptedReading(EvidenceReading):
    """A reading matched to exactly one requested core evidence record.

    The owner names a reading by evidence ID only, and an ID is unique only within
    its source request. Acceptance binds the reading to the full `EvidenceKey`, so
    every later lookup, assessment source and parent identity uses the key.
    """

    source_request: RequestId

    @property
    def key(self) -> EvidenceKey:
        """The run-wide identity of the evidence this reading decodes."""
        return EvidenceKey(source_request=self.source_request, evidence_id=self.evidence_id)


def reading_of[R: EvidenceReading](readings: tuple[R, ...], kind: EvidenceKind) -> R | None:
    """The latest decoded reading of one evidence kind, or None when absent."""
    matching = [item for item in readings if item.kind is kind]
    return matching[-1] if matching else None


def accept_readings(
    readings: tuple[EvidenceReading, ...], requested: tuple[EvidenceRef, ...]
) -> tuple[AcceptedReading, ...] | str:
    """Bind each reading to the one requested record it decodes, or say why not.

    A reading must name a requested evidence ID that no other requested record
    shares, at most once, with the record's own kind, and may claim a pass only for
    a record whose observation succeeded.
    """
    accepted: list[AcceptedReading] = []
    for reading in readings:
        matches = tuple(item for item in requested if item.evidence_id == reading.evidence_id)
        if len(matches) != 1:
            return f"reading {reading.evidence_id.root} names no single requested record"
        (ref,) = matches
        if reading.kind is not ref.kind:
            return f"reading {reading.evidence_id.root} has kind {reading.kind.value}"
        if reading.passed and ref.status is not ObservationStatus.SUCCEEDED:
            return f"reading {reading.evidence_id.root} passed over a {ref.status.value} record"
        if any(item.evidence_id == reading.evidence_id for item in accepted):
            return f"reading {reading.evidence_id.root} repeated"
        accepted.append(AcceptedReading(**reading.model_dump(), source_request=ref.source_request))
    return tuple(accepted)


def judged_evidence(view: RunView, scope: Scope) -> tuple[EvidenceRef, ...]:
    """The trusted evaluations a workstream's roles measured, oldest first.

    A judge reviews the candidate its implementer built in the same scope, and an agent measures
    it through the evaluation tool; core records each result as trusted `local-validation`
    evidence of that scope. A prompt cites these as ``cited_evidence`` so the runtime states
    what they measured (value, unit, direction, verdict) from the evidence ledger.
    """
    held = (
        item
        for item in view.measurements
        if item.provenance == "trusted"
        and item.purpose == "local-validation"
        and item.scope == scope
    )
    return tuple(sorted(held, key=lambda item: item.observation_sequence))
