"""Thin journal mirror: pure projection followed by content and record writes."""

from __future__ import annotations

from dataclasses import replace
from importlib.metadata import version
from typing import TYPE_CHECKING, Self

import vs_proto.api as p
from vibesys.events import CoreEvent
from vibesys.records.api import MappedEvent, MappingState, map_event
from vs_decode.api import Unreadable, decode_json
from vs_runlog.api import fold_skeleton

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from types import TracebackType

    from vs_runlog.api import FileRunContent, FileRunLogWriter

# A journal observation can contain large content; the record cap applies only
# after its content has been moved to blobs and transcript streams.
JOURNAL_EVENT_MAX_BYTES = 64 << 20


def _decode(payload: bytes) -> CoreEvent:
    event = decode_json(payload, CoreEvent, source="journal tee", max_bytes=JOURNAL_EVENT_MAX_BYTES)
    if isinstance(event, Unreadable):
        raise TypeError(event.details("journal event"))
    return event


def _epoch_clocks(records: tuple[p.Record, ...]) -> tuple[float, tuple[tuple[float, float], ...]]:
    """Older records lack clock; their wall times define each epoch's elapsed time."""
    clock = 0.0
    epochs: dict[int, tuple[float, float]] = {}
    for record in records:
        if record.at is None:
            continue
        at = record.at.to_datetime().timestamp()
        origin, offset = epochs.setdefault(record.epoch, (at, clock))
        clock = max(clock, record.clock if record.has_field("clock") else offset + at - origin)
    return clock, tuple(epochs.values())


class RunLogTee:
    """One process's additive mirror; JSONL remains authoritative on failure."""

    def __init__(
        self,
        writer: FileRunLogWriter,
        content: FileRunContent,
        run_id: str,
        *,
        history: Callable[[], Iterable[CoreEvent]] = tuple,
        checkout: tuple[str, bool] = ("", False),
    ) -> None:
        """Own a writer; delay replay until the journal has attached its history."""
        self._writer = writer
        self._content = content
        self._run_id = run_id
        self._history = history
        self._state: MappingState | None = None
        self._tree_hash, self._dirty = checkout

    def _initialize(self, event: CoreEvent) -> MappingState:
        state = MappingState(self._run_id, vibesys_version=version("vibesys"), native_intents=True)
        for historical in self._history():
            result = map_event(state, historical)
            self._write_content(result)
            state = result.state
        records = self._writer.read()
        last = records[-1] if records else None
        ended = any(
            r.epoch == self._writer.epoch
            and r.body is not None
            and r.body.field == "lifecycle"
            and r.body.value.state is not None
            and r.body.value.state.field == "ended"
            for r in records
        )
        clock, epochs = _epoch_clocks(records)
        if last is not None and not ended:
            self._writer.append(
                p.Record(
                    seq=self._writer.sequence + 1,
                    epoch=self._writer.epoch,
                    at=p.Timestamp.from_datetime(event.timestamp),
                    clock=clock,
                    body=p.Oneof(
                        "lifecycle",
                        p.Lifecycle(
                            state=p.Oneof(
                                "ended",
                                p.Ended(
                                    outcome=p.Outcome.INTERRUPTED,
                                    resumable=True,
                                    interruption_reason="previous writer exited without ending its epoch",
                                ),
                            )
                        ),
                    ),
                )
            )
        return replace(
            state,
            sequence=self._writer.sequence,
            epoch=self._writer.epoch + 1,
            header_written=False,
            epoch_header=None,
            epoch_ended=False,
            ending=None,
            origin=event.timestamp.timestamp(),
            clock=clock,
            clock_offset=clock,
            tree_hash=self._tree_hash,
            dirty=self._dirty,
            wall_span_clock=True,
            clock_epochs=(*epochs, (event.timestamp.timestamp(), clock)),
            spans=tuple(
                (key, history.versions[-1].span.to_binary())
                for key, history in fold_skeleton(records, log_offset=0).spans.items()
                if history.versions and history.versions[-1].span is not None
            ),
        )

    def append(self, payload: bytes) -> None:
        """Project committed JSONL bytes, writing referenced content before records."""
        try:
            event = _decode(payload)
            if self._state is None:
                self._state = self._initialize(event)
            result = map_event(self._state, event)
            self._write_content(result)
            for record in result.records:
                self._writer.append(record)
            self._state = result.state
        except BaseException:
            self.close()
            raise

    def _write_content(self, result: MappedEvent) -> None:
        for blob in result.blobs:
            self._content.write_blob(blob.sha256, blob.content)
        for stream in result.streams:
            self._content.append_stream(stream.id, stream.offset, stream.content)

    def commit(self) -> None:
        """Durably commit pending typed records at an explicit run boundary."""
        self._writer.commit()

    def close(self) -> None:
        """Release the mirror's append handle; safe after a failed append."""
        self._writer.close()

    def __enter__(self) -> Self:
        """Own the mirror for one conversion scope."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close on successful and exceptional exits alike."""
        self.close()
