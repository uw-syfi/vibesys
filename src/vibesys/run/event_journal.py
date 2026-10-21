"""Core-event binding for the runtime-owned durable journal."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from vibesys.events import CoreEvent, CoreEventData, CoreEventType, make_core_event
from vs_proto.api import Timestamp
from vs_runlog.api import LegacyRunLogTee
from vs_runtime.api.infrastructure import DurableEventJournal, EventCodec
from vs_sim.api import Clock, Threads, utc_datetime

if TYPE_CHECKING:
    from vs_project.api import Project


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

    def __init__(self, clock: Clock, threads: Threads) -> None:
        """Create an unattached journal for ``core-events.jsonl`` stamped by epoch ``clock``."""
        super().__init__(_CoreEventCodec(), filename="core-events.jsonl", threads=threads)
        self._clock = clock
        self._runlog_configured = False
        self._runlog: LegacyRunLogTee | None = None

    def enable_runlog(self, project: Project, run_id: str) -> None:
        """Enable the additive transport and publish discovery once for this process."""
        if self._runlog_configured:
            return
        self._runlog_configured = True
        try:
            path = project.run_log_path_for(project.root, run_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            # Phase 1 has no checkout provenance at launch. Leave it unknown;
            # phase 3 (#3332) records provenance in EpochStarted at the launch boundary.
            self._runlog = LegacyRunLogTee(path, run_id, self._runlog_timestamp)
            project.publish_run_for(project.root, run_id)
            self.set_append_sink(self._runlog.append)
        except Exception:
            self.close()
            logging.getLogger(__name__).exception("Run log tee disabled during initialization")

    def close(self) -> None:
        """Release the additive transport's append handle on run teardown."""
        if self._runlog is not None:
            self._runlog.close()
            self._runlog = None

    def _runlog_timestamp(self) -> Timestamp:
        return Timestamp.from_datetime(utc_datetime(self._clock))

    def emit(
        self,
        event_type: CoreEventType,
        text: str = "",
        *,
        data: CoreEventData | None = None,
        **fields: object,
    ) -> CoreEvent:
        """Create, record, and publish one semantic core event."""
        return self.record(
            make_core_event(
                event_type, text, timestamp=utc_datetime(self._clock), data=data, **fields
            )
        )

    def has_recorded(self, event_type: CoreEventType) -> bool:
        """Whether an event of ``event_type`` was recorded, this process's or an earlier one's."""
        return any(event.type is event_type for event in self.history())
