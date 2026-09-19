"""Project performance-plot context from authoritative run state.

Like the experiment log, this is a one-way projection: recorded measurement
facts and manifest objectives are copied onto the wire, never recomputed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from server.wire import enums
from server.wire.v2 import responses_pb2

if TYPE_CHECKING:
    from vibesys.loops.agent.model import AgentRunState, HypothesisMeasurement

# The objective document is operator-authored markdown of arbitrary length,
# and the payload must stay bounded, so only one capped paragraph is sent.
_DESCRIPTION_LIMIT = 280


def build_performance_context(
    state: AgentRunState | None,
    *,
    objectives: tuple[str, ...],
    objective_description: str | None = None,
) -> responses_pb2.PerformanceContext | None:
    """Assemble the /perf context, preferring the newest official measurement.

    Before any measurement exists the manifest objectives alone can name the
    metric and its direction, so the section can render from round zero.
    """
    measurement = _latest_measurement(state)
    metric = measurement.metric if measurement is not None else primary_objective_metric(objectives)
    if metric is None and objective_description is None:
        return None
    direction = measurement.direction if measurement is not None else None
    if direction is None and metric is not None:
        direction = metric_directions(objectives).get(metric)
    return responses_pb2.PerformanceContext(
        objective_metric=metric,
        objective_unit=measurement.unit if measurement is not None else None,
        objective_direction=enums.optional_number(responses_pb2.ObjectiveDirection, direction),
        # Baseline facts are copied as one tuple from the same measurement so
        # the value can never pair with another comparison's round or commit.
        objective_baseline_value=measurement.baseline_value if measurement is not None else None,
        objective_baseline_round=measurement.baseline_round if measurement is not None else None,
        objective_baseline_commit=measurement.baseline_commit if measurement is not None else None,
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


def _latest_measurement(state: AgentRunState | None) -> HypothesisMeasurement | None:
    if state is None:
        return None
    latest: HypothesisMeasurement | None = None
    for hypothesis in state.hypotheses:
        measurement = hypothesis.measurement
        if measurement is None:
            continue
        if latest is None or measurement.round > latest.round:
            latest = measurement
    return latest
