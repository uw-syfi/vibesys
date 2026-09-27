"""VibeSys policy values and semantic event projection for evaluations."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

from vibesys.events import (
    CoreEventType,
    EventStatus,
    GateFinishedData,
    GateKind,
    GateStartedData,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vibesys.events import CoreEventData


class _GateEvents(Protocol):
    """Structural event-journal surface used by gate projection."""

    def emit(
        self,
        event_type: CoreEventType,
        *,
        data: CoreEventData,
        status: EventStatus | None = None,
        round_label: str | None = None,
    ) -> object:
        """Publish one typed gate fact."""
        ...


GATE_LOG_TAIL_CHARS = 1000
GATE_FEEDBACK_TAIL_CHARS = 4000


def emit_gate_started(
    events: _GateEvents,
    gate: GateKind,
    *,
    recipe: str | None = None,
    command: str | None = None,
    round_label: str | None = None,
) -> None:
    """Publish that one framework gate began evaluating a candidate."""
    events.emit(
        CoreEventType.GATE_STARTED,
        data=GateStartedData(gate=gate, recipe=recipe, command=command),
        status=EventStatus.ACTIVE,
        round_label=round_label,
    )


def emit_gate_finished(
    events: _GateEvents,
    data: GateFinishedData,
    *,
    passed: bool,
    round_label: str | None = None,
) -> None:
    """Publish one framework gate outcome."""
    events.emit(
        CoreEventType.GATE_FINISHED,
        data=data,
        status=EventStatus.COMPLETED if passed else EventStatus.FAILED,
        round_label=round_label,
    )


@dataclass(frozen=True, slots=True)
class FrameworkBenchmarkOutcome:
    """Policy-interpreted benchmark headline and full measured row."""

    feedback: str | None = None
    metric_name: str | None = None
    metric_value: float | None = None
    metric_direction: Literal["max", "min"] | None = None
    metric_unit: str | None = None
    row: Mapping[str, float] | None = None
