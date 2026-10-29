"""Thin journal mirror: pure projection followed by content and record writes."""

from __future__ import annotations

from dataclasses import replace
from importlib.metadata import version
from typing import TYPE_CHECKING, Self

import vs_proto.api as p
from vibesys.events import CoreEvent
from vibesys.records.api import MappedEvent, MappingState, epoch_header, map_event
from vs_decode.api import Unreadable, decode_json
from vs_runlog.api import current_epoch, epoch_has_ended, fold_skeleton

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from datetime import datetime
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
        epoch_clock = record.clock if record.has_field("clock") else clock
        origin, offset = epochs.setdefault(record.epoch, (at, epoch_clock))
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
        checkout: tuple[str, bool] | None = None,
    ) -> None:
        """Own a writer; delay replay until the journal has attached its history.

        A live journal supplies its checkout (an empty hash explicitly means
        unknown). Offline conversion translates the same wall clock as the live mirror.
        """
        self._writer = writer
        self._content = content
        self._run_id = run_id
        self._history = history
        self._state: MappingState | None = None
        self._live = checkout is not None
        self._project_root = ""
        self._tree_hash, self._dirty = ("", False) if checkout is None else checkout

    def remember_project_root(self, project_root: str) -> None:
        """Record the canonical project root before the first event is mirrored."""
        self._project_root = project_root

    def publish_successor(self, at: datetime) -> None:
        """Publish this live writer's epoch before any later observation.

        Relaunch confirms from this record. A journal replay leaves opening to
        its first event, which already carries the declaration.
        """
        if not self._live or self._state is not None:
            return
        state = self._initialize(at)
        declared = epoch_header(state, at)
        self._write_content(declared)
        for record in declared.records:
            self._writer.append(record)
        self._state = declared.state

    def has_open_epoch(self) -> bool:
        """Whether this process published an epoch it has not ended."""
        return self._state is not None and not self._state.epoch_ended

    def _initialize(self, at: datetime) -> MappingState:
        state = MappingState(
            self._run_id,
            vibesys_version=version("vibesys"),
            native_intents=self._live,
            project_root=self._project_root,
        )
        for historical in self._history():
            result = map_event(state, historical)
            self._write_content(result)
            state = result.state
        records = self._writer.read()
        intents = {
            record.body.value.control_id: record.body.value.to_binary()
            for record in records
            if record.body is not None and record.body.field == "intent"
        }
        last = records[-1] if records else None
        ended = epoch_has_ended(current_epoch(records))
        clock, epochs = _epoch_clocks(records)
        if last is not None and not ended:
            self._writer.append(
                p.Record(
                    seq=self._writer.sequence + 1,
                    epoch=self._writer.epoch,
                    at=p.Timestamp.from_datetime(at),
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
            origin=at.timestamp(),
            clock=clock,
            clock_offset=clock,
            tree_hash=self._tree_hash,
            dirty=self._dirty,
            wall_span_clock=True,
            clock_epochs=(*epochs, (at.timestamp(), clock)),
            intents=tuple(intents.items()),
            spans=tuple(
                (key, history.versions[-1].span.to_binary())
                for key, history in fold_skeleton(records, log_offset=0).spans.items()
                if history.versions and history.versions[-1].span is not None
            ),
        )

    @property
    def epoch(self) -> int:
        """The public writer epoch, including startup before the core lease is acquired."""
        return self._writer.current_epoch.number

    def intent(self, control_id: str) -> p.IntentState | None:
        """Return a previously recorded outcome without exposing mutable projection state."""
        if self._state is not None:
            payload = dict(self._state.intents).get(control_id)
            return None if payload is None else p.IntentState.from_binary(payload)
        for record in reversed(self._writer.read()):
            body = record.body
            if body and body.field == "intent" and body.value.control_id == control_id:
                return body.value
        return None

    def append(self, payload: bytes) -> None:
        """Project committed JSONL bytes, writing referenced content before records."""
        try:
            event = _decode(payload)
            if self._state is None:
                self._state = self._initialize(event.timestamp)
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
