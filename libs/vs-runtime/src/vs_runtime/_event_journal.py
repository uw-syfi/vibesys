"""Generic durable event journal mechanics."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from vs_sim.api import Threads


class EventCodec[EventT](Protocol):
    """Serialize events and assign their journal identity."""

    def decode(self, data: bytes) -> EventT:
        """Decode one JSONL record, raising ``ValueError`` when invalid."""
        ...

    def encode(self, event: EventT) -> bytes:
        """Encode one event without a trailing newline."""
        ...

    def sequence(self, event: EventT) -> int:
        """Return the event's durable sequence number."""
        ...

    def stamp(self, event: EventT, *, sequence: int, stream_id: str) -> EventT:
        """Return *event* carrying its durable journal identity."""
        ...


type EventSubscriber[EventT] = Callable[[EventT], None]


@dataclass(frozen=True, slots=True)
class _PendingWrite:
    path: Path
    offset: int
    payload: bytes


class DurableEventJournal[EventT]:
    """Persist codec-defined events while supporting replay and subscriptions."""

    def __init__(self, codec: EventCodec[EventT], *, filename: str, threads: Threads) -> None:
        """Create an unattached journal for one event codec and file name, locked by ``threads``."""
        if not filename or Path(filename).name != filename:
            message = "journal filename must be one non-empty path component"
            raise ValueError(message)
        self._codec = codec
        self._filename = filename
        self._condition = threads.condition(threads.rlock())
        self._path: Path | None = None
        self._stream_id = ""
        self._events: list[EventT] = []
        self._pending: list[EventT] = []
        self._pending_write: _PendingWrite | None = None
        self._subscribers: tuple[EventSubscriber[EventT], ...] = ()

    @property
    def path(self) -> Path | None:
        """Return the durable event path after attachment."""
        with self._condition:
            return self._path

    @property
    def latest_sequence(self) -> int:
        """Return the latest durable sequence number."""
        with self._condition:
            return self._codec.sequence(self._events[-1]) if self._events else 0

    def attach(self, directory: Path, stream_id: str) -> None:
        """Attach to the configured JSONL file and flush pending events."""
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self._filename
        with self._condition:
            if self._path is not None and self._pending and self._path != path:
                message = "cannot change journal path while an attachment retry is pending"
                raise RuntimeError(message)
            if self._path is not None and self._pending and self._stream_id != stream_id:
                message = "cannot change journal stream while an attachment retry is pending"
                raise RuntimeError(message)
            pending_write_persisted = self._pending_write_persisted_locked(path)
            if self._path == path and not self._pending and pending_write_persisted is None:
                self._stream_id = stream_id
                return
            existing = self._read_path(path)
            if pending_write_persisted:
                del self._pending[0]
            if pending_write_persisted is not None:
                self._pending_write = None
            self._path = path
            self._stream_id = stream_id
            self._events = existing
            flushed = 0
            try:
                for event in self._pending:
                    self._append_pending_locked(event)
                    flushed += 1
            finally:
                if flushed:
                    del self._pending[:flushed]
            self._condition.notify_all()

    def subscribe(
        self,
        subscriber: EventSubscriber[EventT],
        *,
        replay: bool = False,
    ) -> Callable[[], None]:
        """Register a live subscriber and return an idempotent unsubscriber."""
        with self._condition:
            self._subscribers = (*self._subscribers, subscriber)
            history = tuple(self._events) if replay else ()
        for event in history:
            subscriber(event)

        def unsubscribe() -> None:
            with self._condition:
                self._subscribers = tuple(
                    candidate for candidate in self._subscribers if candidate is not subscriber
                )

        return unsubscribe

    def record(self, event: EventT) -> EventT:
        """Record an event and publish it exactly once."""
        with self._condition:
            if self._path is None or self._pending:
                self._pending.append(event)
                recorded = event
            else:
                recorded = self._append_locked(event, notify=True)
            subscribers = self._subscribers
        for subscriber in subscribers:
            subscriber(recorded)
        return recorded

    def read(self, after_sequence: int = 0) -> list[EventT]:
        """Return durable events after a cursor."""
        with self._condition:
            return [event for event in self._events if self._codec.sequence(event) > after_sequence]

    def history(self) -> list[EventT]:
        """Return every event recorded so far: the durable ones, then any awaiting attachment."""
        with self._condition:
            return [*self._events, *self._pending]

    def wait(self, after_sequence: int, timeout: float | None = None) -> list[EventT]:
        """Wait until durable events exist after a cursor."""
        with self._condition:
            events = self.read(after_sequence)
            if events:
                return events
            self._condition.wait(timeout)
            return self.read(after_sequence)

    def _append_locked(self, event: EventT, *, notify: bool) -> EventT:
        recorded, payload = self._prepare_locked(event)
        return self._write_locked(recorded, payload, notify=notify)

    def _append_pending_locked(self, event: EventT) -> EventT:
        recorded, payload = self._prepare_locked(event)
        if self._path is None:
            message = "cannot append a durable event before attachment"
            raise RuntimeError(message)
        offset = self._path.stat().st_size if self._path.exists() else 0
        self._pending_write = _PendingWrite(path=self._path, offset=offset, payload=payload)
        recorded = self._write_locked(recorded, payload, notify=False)
        self._pending_write = None
        return recorded

    def _prepare_locked(self, event: EventT) -> tuple[EventT, bytes]:
        sequence = self._codec.sequence(self._events[-1]) + 1 if self._events else 1
        recorded = self._codec.stamp(event, sequence=sequence, stream_id=self._stream_id)
        return recorded, self._codec.encode(recorded) + b"\n"

    def _write_locked(self, recorded: EventT, payload: bytes, *, notify: bool) -> EventT:
        if self._path is None:
            message = "cannot append a durable event before attachment"
            raise RuntimeError(message)
        with self._path.open("ab") as stream:
            written = stream.write(payload)
            if written != len(payload):
                message = f"incomplete event journal write: {written} of {len(payload)} bytes"
                raise OSError(message)
        self._events.append(recorded)
        if notify:
            self._condition.notify_all()
        return recorded

    def _pending_write_persisted_locked(self, path: Path) -> bool | None:
        """Inspect an ambiguous append without consuming its retry state."""
        pending_write = self._pending_write
        if pending_write is None:
            return None
        if pending_write.path != path:
            message = "cannot retry an interrupted journal write at a different path"
            raise RuntimeError(message)
        try:
            with path.open("rb") as stream:
                stream.seek(pending_write.offset)
                persisted = stream.read(len(pending_write.payload))
        except FileNotFoundError:
            persisted = b""
        complete = pending_write.payload
        without_delimiter = complete[:-1] if len(complete) > 1 else complete
        return persisted in (complete, without_delimiter)

    def _read_path(self, path: Path) -> list[EventT]:
        if not path.exists():
            return []
        contents = path.read_bytes()
        lines = contents.splitlines(keepends=True)
        events: list[EventT] = []
        valid_end = 0
        for index, line in enumerate(lines):
            try:
                events.append(self._codec.decode(line))
            except ValueError:
                if index == len(lines) - 1:
                    with path.open("r+b") as stream:
                        stream.truncate(valid_end)
                    return events
                raise
            valid_end += len(line)
        if contents and not contents.endswith((b"\n", b"\r")):
            with path.open("ab") as stream:
                stream.write(b"\n")
        return events
