"""The run trace as the server holds it: the spans its span events carry, folded by span id.

The runtime records spans and the run emits one ``span_started``/``span_updated``/``span_ended``
event per change, each with the whole span. This projection folds those events, live and on
replay, so the snapshot can carry the current spans and the server can name the agent-turn span
of the turn an agent's own records (its usage) speak of by invocation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from server.events import EventType, SpanEndedData, SpanStartedData, SpanUpdatedData

if TYPE_CHECKING:
    from server.events import EventHeader, RunEvent, SpanId, WireSpan

SPAN_EVENTS = frozenset({EventType.SPAN_STARTED, EventType.SPAN_UPDATED, EventType.SPAN_ENDED})


class TraceProjection:
    """The run's spans in the order they started, each as its latest event left it.

    Called under the journal's lock (it is a journal listener), so it keeps no lock of its own.
    """

    def __init__(self) -> None:
        """Start with no spans."""
        self._spans: dict[SpanId, WireSpan] = {}
        self._turns: dict[str, SpanId] = {}

    @staticmethod
    def replay_filter(header: EventHeader) -> bool:
        """Whether a stored event is one this projection folds."""
        return header.type in SPAN_EVENTS

    def observe(self, event: RunEvent) -> None:
        """Fold one span event; any other event leaves the projection as it is."""
        data = event.data
        if not isinstance(data, SpanStartedData | SpanUpdatedData | SpanEndedData):
            return
        span = data.span
        self._spans[span.id] = span
        if span.invocation is not None:
            self._turns[span.invocation] = span.id

    def spans(self) -> list[WireSpan]:
        """Every span of the run, each after its parent."""
        return list(self._spans.values())

    def turn_span(self, invocation: str | None) -> SpanId | None:
        """The agent-turn span that ran as ``invocation``, if the run recorded one."""
        return None if invocation is None else self._turns.get(invocation)
