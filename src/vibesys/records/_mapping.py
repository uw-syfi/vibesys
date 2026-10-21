"""Exhaustive disposition of the durable core-event union."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

import vibesys.events as e
import vs_proto.api as p
from vibesys.records import _agents, _capacity, _control, _lifecycle, _strategy
from vibesys.records._common import Projection
from vibesys.records._spans import project_span
from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.records._types import MappedEvent, MappingState

# Every deletion in the event inventory is named here. No default drop exists.
DROPS: dict[e.CoreEventType, str] = {
    e.CoreEventType.INVOCATION_STARTED: "duplicate of agent_execution_started",
    e.CoreEventType.INVOCATION_FINISHED: "duplicate of agent_execution_finished",
    e.CoreEventType.AGENT_EXECUTION_ACTIVITY_CHANGED: "derived from transcript tail",
    e.CoreEventType.PHASE_STARTED: "turn and workstream spans own phase work",
    e.CoreEventType.PHASE_FINISHED: "turn and workstream spans own phase work",
    e.CoreEventType.TODO_UPDATE: "OQ13: no consumer",
    e.CoreEventType.USAGE_UPDATE: "OQ13: in-flight context snapshot has no consumer",
    e.CoreEventType.EXPERIMENTS_CHANGED: "refetch hint; experiments are a client span fold",
    e.CoreEventType.ITERATIONS_CHANGED: "refetch hint; iterations are a client span fold",
    e.CoreEventType.JUDGE_RESULT: "no producer; workstream verdict owns the result",
    e.CoreEventType.BENCHMARK_RESULT: "no producer; gate metric points own readings",
    e.CoreEventType.RUN_CONFIGURED: "no producer; epoch describes configuration",
    e.CoreEventType.ACTION_QUEUED: "pending internal action, not an applied or refused intent",
    e.CoreEventType.ACTION_APPLIED: "legacy action text has no request identity; phase 3 supplies typed actions",
    e.CoreEventType.WORKSPACE_SNAPSHOT: "workstream attrs own revision and parent_revision",
}


def _span(projection: Projection) -> None:
    data = projection.event.data
    if not isinstance(data, (e.SpanStartedData, e.SpanUpdatedData, e.SpanEndedData)):
        message = f"{projection.event.type}: requires a whole span payload"
        raise TypeError(message)
    changes = {
        e.CoreEventType.SPAN_STARTED: p.Change.STARTED,
        e.CoreEventType.SPAN_UPDATED: p.Change.UPDATED,
        e.CoreEventType.SPAN_ENDED: p.Change.ENDED,
    }
    project_span(projection, data.span, changes[projection.event.type])


MAPPERS: dict[e.CoreEventType, Callable[[Projection], None]] = {
    **_agents.MAPPERS,
    **_capacity.MAPPERS,
    **_control.MAPPERS,
    **_lifecycle.MAPPERS,
    **_strategy.MAPPERS,
    e.CoreEventType.SPAN_STARTED: _span,
    e.CoreEventType.SPAN_UPDATED: _span,
    e.CoreEventType.SPAN_ENDED: _span,
}
if DROPS.keys() & MAPPERS.keys():
    message = "journal event kinds must have exactly one disposition"
    raise RuntimeError(message)
DISPOSITIONS = MappingProxyType({**DROPS, **dict.fromkeys(MAPPERS, "typed record")})


def map_event(state: MappingState, event: e.CoreEvent) -> MappedEvent:
    """Project one event, raising on absent dispositions or invalid typed content."""
    projection = Projection(state, event)
    if not state.header_written and event.type is not e.CoreEventType.RUN_STARTED:
        _lifecycle.header(projection)
    projection.final = event.type in {
        e.CoreEventType.RUN_FINISHED,
        e.CoreEventType.RUN_FAILED,
        e.CoreEventType.STOPPED,
    }
    if event.type not in DROPS:
        MAPPERS[event.type](projection)
    if projection.final or isinstance(
        event.data, (e.SpanStartedData, e.SpanUpdatedData, e.SpanEndedData)
    ):
        pending, projection.pending = projection.pending, []
        for payload in pending:
            deferred = decode_json(
                payload, e.CoreEvent, source="deferred journal event", max_bytes=64 << 20
            )
            if isinstance(deferred, Unreadable):
                raise TypeError(deferred.details("deferred journal event"))
            projection.event = deferred
            MAPPERS[deferred.type](projection)
        projection.event = event
    return projection.result()
