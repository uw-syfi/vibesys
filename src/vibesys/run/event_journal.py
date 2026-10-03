"""Core-event binding for the runtime-owned durable journal."""

from __future__ import annotations

from vibesys.events import CoreEvent, CoreEventData, CoreEventType, make_core_event
from vs_runtime.api.infrastructure import DurableEventJournal, EventCodec


class _CoreEventCodec(EventCodec[CoreEvent]):
    """Bind VibeSys's semantic event envelope to generic JSONL mechanics."""

    def decode(self, data: bytes) -> CoreEvent:
        """Decode one persisted core event."""
        return CoreEvent.model_validate_json(data)

    def encode(self, event: CoreEvent) -> bytes:
        """Encode one core event without a trailing newline."""
        return event.model_dump_json().encode()

    def sequence(self, event: CoreEvent) -> int:
        """Return the durable cursor carried by *event*."""
        return event.sequence

    def stamp(self, event: CoreEvent, *, sequence: int, stream_id: str) -> CoreEvent:
        """Assign the journal cursor and run identity."""
        return event.model_copy(update={"sequence": sequence, "run_id": stream_id})


class EventJournal(DurableEventJournal[CoreEvent]):
    """Persist VibeSys core events using runtime-owned journal mechanics."""

    def __init__(self) -> None:
        """Create an unattached journal for ``core-events.jsonl``."""
        super().__init__(_CoreEventCodec(), filename="core-events.jsonl")

    def emit(
        self,
        event_type: CoreEventType,
        text: str = "",
        *,
        data: CoreEventData | None = None,
        **fields: object,
    ) -> CoreEvent:
        """Create, record, and publish one semantic core event."""
        return self.record(make_core_event(event_type, text, data=data, **fields))
