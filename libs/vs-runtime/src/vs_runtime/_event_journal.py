"""Generic durable event journal mechanics."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol


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


class DurableEventJournal[EventT]:
    """Persist codec-defined events while supporting replay and subscriptions."""

    def __init__(self, codec: EventCodec[EventT], *, filename: str) -> None:
        """Create an unattached journal for one event codec and file name."""
        if not filename or Path(filename).name != filename:
            message = "journal filename must be one non-empty path component"
            raise ValueError(message)
        self._codec = codec
        self._filename = filename
        self._condition = threading.Condition(threading.RLock())
        self._path: Path | None = None
        self._stream_id = ""
        self._events: list[EventT] = []
        self._pending: list[EventT] = []
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
            if self._path == path:
                self._stream_id = stream_id
                return
            existing = self._read_path(path)
            pending = self._pending
            self._pending = []
            self._path = path
            self._stream_id = stream_id
            self._events = existing
            for event in pending:
                self._append_locked(event, notify=False)
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
            if self._path is None:
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

    def wait(self, after_sequence: int, timeout: float | None = None) -> list[EventT]:
        """Wait until durable events exist after a cursor."""
        with self._condition:
            events = self.read(after_sequence)
            if events:
                return events
            self._condition.wait(timeout)
            return self.read(after_sequence)

    def _append_locked(self, event: EventT, *, notify: bool) -> EventT:
        sequence = self._codec.sequence(self._events[-1]) + 1 if self._events else 1
        recorded = self._codec.stamp(event, sequence=sequence, stream_id=self._stream_id)
        if self._path is None:
            message = "cannot append a durable event before attachment"
            raise RuntimeError(message)
        with self._path.open("ab") as stream:
            stream.write(self._codec.encode(recorded) + b"\n")
        self._events.append(recorded)
        if notify:
            self._condition.notify_all()
        return recorded

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
