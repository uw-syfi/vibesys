"""Objective axes and metric comparison shared by optimization loops.

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

``MetricComparison`` itself is defined in ``vs_loop_state`` and re-exported
here: a round record stores the comparison the framework made, and that library
must not import loop code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator
from pydantic.dataclasses import dataclass

from vs_loop_state import MetricComparison

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


__all__ = ["Measurement", "MetricComparison", "MetricSpace", "Objective"]


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
            raise ValueError("objective names must be unique")  # noqa: TRY003  # tracked: #288
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
