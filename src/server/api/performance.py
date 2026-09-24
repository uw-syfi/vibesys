"""Project performance-plot context from a run's `RunView`.

Like the experiment log, this is a one-way projection: recorded measurement
facts and manifest objectives are copied onto the wire, never recomputed.
`vibesys.api.agent`'s `HypothesisView` already carries the headline measurement
each hypothesis recorded (see `vibesys.loops.agent.readmodel`); this module only
selects the newest one and reshapes it into the wire DTO.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from server.api.protocol import PerformanceContext
from vibesys.api.agent import agent_projection

if TYPE_CHECKING:
    from vibesys.api import RunView
    from vibesys.api.agent import HypothesisView

# The objective document is operator-authored markdown of arbitrary length,
# and the payload must stay bounded, so only one capped paragraph is sent.
_DESCRIPTION_LIMIT = 280


def build_performance_context(
    run_view: RunView | None,
    *,
    objectives: tuple[str, ...],
    objective_description: str | None = None,
) -> PerformanceContext | None:
    """Assemble the /perf context, preferring the newest official measurement.

    Before any measurement exists the manifest objectives alone can name the
    metric and its direction, so the section can render from round zero.
    """
    measurement = _latest_measurement(run_view)
    metric = (
        measurement.perf_metric_name
        if measurement is not None
        else primary_objective_metric(objectives)
    )
    if metric is None and objective_description is None:
        return None
    direction = measurement.perf_direction if measurement is not None else None
    if direction is None and metric is not None:
        direction = metric_directions(objectives).get(metric)
    return PerformanceContext(
        objective_metric=metric,
        objective_unit=measurement.perf_unit if measurement is not None else None,
        objective_direction=direction,
        # Baseline facts are copied as one tuple from the same measurement so
        # the value can never pair with another comparison's round or commit.
        objective_baseline_value=(
            measurement.perf_baseline_value if measurement is not None else None
        ),
        objective_baseline_round=(
            measurement.perf_baseline_round if measurement is not None else None
        ),
        objective_baseline_commit=(
            measurement.perf_baseline_commit if measurement is not None else None
        ),
        objective_description=objective_description,
    )


def summarize_objective(text: str) -> str | None:
    """Return the first prose paragraph of an objective document, bounded."""
    for block in text.split("\n\n"):
        lines = (line.strip() for line in block.splitlines())
        prose = " ".join(line for line in lines if line and not line.startswith("#"))
        if not prose:
            continue
        if len(prose) > _DESCRIPTION_LIMIT:
            prose = prose[: _DESCRIPTION_LIMIT - 1].rstrip() + "…"
        return prose
    return None


def primary_objective_metric(encoded: tuple[str, ...]) -> str | None:
    """Return the first objective's metric name from its encoded form."""
    for value in encoded:
        name, separator, _ = value.rpartition(":")
        metric = name if separator else value
        if metric:
            return metric
    return None


def metric_directions(encoded: tuple[str, ...]) -> dict[str, Literal["max", "min"]]:
    """Decode objective directions stored with an agent run."""
    directions: dict[str, Literal["max", "min"]] = {}
    for value in encoded:
        name, separator, direction = value.rpartition(":")
        if separator and name:
            if direction == "max":
                directions[name] = "max"
            elif direction == "min":
                directions[name] = "min"
    return directions


def _latest_measurement(run_view: RunView | None) -> HypothesisView | None:
    """Return the hypothesis whose headline measurement is the newest.

    Selects by `HypothesisView.perf_metric_round`, the round that produced
    that hypothesis's own measurement, matching the prior
    `HypothesisMeasurement.round` selection field-for-field.
    """
    if run_view is None:
        return None
    projection = agent_projection(run_view)
    if projection is None:
        return None
    latest: HypothesisView | None = None
    latest_round: int | None = None
    for hypothesis in projection.hypotheses:
        round_number = hypothesis.perf_metric_round
        if round_number is None:
            continue
        if latest_round is None or round_number > latest_round:
            latest = hypothesis
            latest_round = round_number
    return latest
