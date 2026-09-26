"""Durable, subscribable journal for core run events."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from pydantic import ValidationError

from vibesys.events import CoreEvent, CoreEventData, CoreEventType, make_core_event

if TYPE_CHECKING:
    from pathlib import Path
EventSubscriber = Callable[[CoreEvent], None]


class EventJournal:
    """Persist core events while supporting replay and live subscriptions."""

    def __init__(self) -> None:
        """Create an unattached journal with no subscribers."""
        self._condition = threading.Condition(threading.RLock())
        self._path: Path | None = None
        self._run_id = ""
        self._events: list[CoreEvent] = []
        self._pending: list[CoreEvent] = []
        self._subscribers: tuple[EventSubscriber, ...] = ()

    @property
    def path(self) -> Path | None:
        """Return the durable event path after attachment."""
        with self._condition:
            return self._path

    @property
    def latest_sequence(self) -> int:
        """Return the latest durable sequence number."""
        with self._condition:
            return self._events[-1].sequence if self._events else 0

    def attach(self, log_dir: Path, run_id: str) -> None:
        """Attach to ``core-events.jsonl`` and flush pre-attachment events."""
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "core-events.jsonl"
        with self._condition:
            if self._path == path:
                self._run_id = run_id
                return
            existing = self._read_path(path)
            pending = self._pending
            self._pending = []
            self._path = path
            self._run_id = run_id
            self._events = existing
            for event in pending:
                self._append_locked(event, notify=False)
            self._condition.notify_all()

    def subscribe(self, subscriber: EventSubscriber, *, replay: bool = False) -> Callable[[], None]:
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

    def emit(
        self,
        event_type: CoreEventType,
        text: str = "",
        *,
        data: CoreEventData | None = None,
        **fields: object,
    ) -> CoreEvent:
        """Create, record, and publish one core event."""
        return self.record(make_core_event(event_type, text, data=data, **fields))

    def record(self, event: CoreEvent) -> CoreEvent:
        """Record an existing event and publish it exactly once."""
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

    def read(self, after_sequence: int = 0) -> list[CoreEvent]:
        """Return durable events after a cursor."""
        with self._condition:
            return [event for event in self._events if event.sequence > after_sequence]

    def wait(self, after_sequence: int, timeout: float | None = None) -> list[CoreEvent]:
        """Wait until durable events exist after a cursor."""
        with self._condition:
            events = [event for event in self._events if event.sequence > after_sequence]
            if events:
                return events
            self._condition.wait(timeout)
            return [event for event in self._events if event.sequence > after_sequence]

    def _append_locked(self, event: CoreEvent, *, notify: bool) -> CoreEvent:
        sequence = self._events[-1].sequence + 1 if self._events else 1
        recorded = event.model_copy(
            update={"sequence": sequence, "run_id": self._run_id},
        )
        if self._path is None:
            message = "cannot append a durable event before attachment"
            raise RuntimeError(message)
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(recorded.model_dump_json() + "\n")
        self._events.append(recorded)
        if notify:
            self._condition.notify_all()
        return recorded

    @staticmethod
    def _read_path(path: Path) -> list[CoreEvent]:
        if not path.exists():
            return []
        contents = path.read_bytes()
        lines = contents.splitlines(keepends=True)
        events: list[CoreEvent] = []
        valid_end = 0
        for index, line in enumerate(lines):
            try:
                events.append(CoreEvent.model_validate_json(line))
            except ValidationError:
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
