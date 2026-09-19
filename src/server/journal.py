"""Durable append/replay journal for server wire events."""

from __future__ import annotations

import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any, Literal

from server.diagnostics import (
    DiagnosticRetryability,
    DiagnosticScope,
    DiagnosticSeverity,
    exception_detail,
    exception_to_diagnostic,
)
from server.events import EventHeader, EventStore, header_from_event
from server.wire import enums, messages
from server.wire.v2 import events_pb2, snapshot_pb2

if TYPE_CHECKING:
    import threading

    from google.protobuf import struct_pb2
    from google.protobuf.message import Message

    from server.wire.v2.common_pb2 import Diagnostic

EventType = events_pb2.EventType
EventStatus = events_pb2.EventStatus
OutputStream = Literal["stdout", "stderr"]
"""Host stream of a captured output line, in the domain's string vocabulary."""

_MAX_EXCEPTION_CHAIN = 8
DIAGNOSTIC_FAILURE_EVENTS = frozenset(
    {
        EventType.EVENT_TYPE_CONFIGURATION_FAILED,
        EventType.EVENT_TYPE_INVOCATION_FINISHED,
        EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED,
        EventType.EVENT_TYPE_PHASE_FINISHED,
        EventType.EVENT_TYPE_RUN_FAILED,
        EventType.EVENT_TYPE_RUN_INTERRUPTED,
    }
)
"""Operational failure events that must carry a diagnostic when FAILED.

Gate and judge outcomes are deliberately absent: a failed gate or judge
verdict is an expected semantic result, not an operational fault.
"""
_NONTERMINAL_FAILURE_EVENTS = frozenset(
    {
        EventType.EVENT_TYPE_INVOCATION_FINISHED,
        EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED,
        EventType.EVENT_TYPE_PHASE_FINISHED,
    }
)
_CANONICAL_LIFECYCLE_EVENTS = frozenset(
    {EventType.EVENT_TYPE_AGENT_EXECUTION_STARTED, EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED}
)
_LEGACY_LIFECYCLE_EVENTS = frozenset(
    {EventType.EVENT_TYPE_INVOCATION_STARTED, EventType.EVENT_TYPE_INVOCATION_FINISHED}
)
_BOOTSTRAP_SPINE_TYPES = frozenset(
    {
        EventType.EVENT_TYPE_RUN_STARTED,
        EventType.EVENT_TYPE_RUN_STATUS_CHANGED,
        EventType.EVENT_TYPE_RUN_FINISHED,
        EventType.EVENT_TYPE_RUN_FAILED,
        EventType.EVENT_TYPE_RUN_INTERRUPTED,
        EventType.EVENT_TYPE_CONFIGURATION_FAILED,
        EventType.EVENT_TYPE_ROUND_FINISHED,
        EventType.EVENT_TYPE_EXPERIMENTS_CHANGED,
        EventType.EVENT_TYPE_CHAT_THREAD_CREATED,
    }
)

EventListener = Callable[[events_pb2.RunEvent], None]
HeaderFilter = Callable[[EventHeader], bool]


class EventJournal:
    """Own event serialization, replay compatibility, and failure identity."""

    def __init__(self, condition: threading.Condition) -> None:
        """Initialize journal state over the shared server condition."""
        self._condition = condition
        self._store: EventStore | None = None
        self._pending_events: list[events_pb2.RunEvent] = []
        self._canonical_execution_ids: set[str] = set()
        self._legacy_invocation_ids: set[str] = set()
        self._error_diagnostics: dict[int, tuple[BaseException, Diagnostic]] = {}
        self._listeners: list[tuple[EventListener, HeaderFilter]] = []
        self.log_dir: Path | None = None

    def add_listener(self, listener: EventListener, *, replay_filter: HeaderFilter) -> None:
        """Register a live append reducer and its selective replay filter."""
        with self._condition:
            self._listeners.append((listener, replay_filter))

    def attach(self, log_dir: Path, *, run_id: str | None = None) -> None:
        """Attach the journal to a durable run event file."""
        log_dir.mkdir(parents=True, exist_ok=True)
        events_path = log_dir / "run-events.jsonl"
        with self._condition:
            previous = self._store
            if previous is not None and previous.path == events_path:
                if run_id is not None:
                    previous.run_id = run_id
                self.log_dir = log_dir
                return
            durable = EventStore(events_path, run_id=run_id or log_dir.parent.name)
            self._index_stored_history(durable)
            pending = previous.read() if previous is not None else self._pending_events
            self._pending_events = []
            # Migrating into an empty log re-appends the retired store's events
            # in order, so every sequence keeps its meaning: the new store
            # continues the same sequence space, and subscriptions that folded
            # it are still correct. Carrying the identity over says so. A
            # nonempty log renumbers those events onto its own tail instead,
            # which is a different space and has to read as one.
            if previous is not None and durable.last_sequence == 0:
                durable.store_id = previous.store_id
            for event in pending:
                self._apply_recorded(durable.append(event))
            self._store = durable
            self.log_dir = log_dir
            started_fresh = previous is None
            if previous is not None:
                previous.notify_change()
        if started_fresh:
            self.record(EventType.EVENT_TYPE_SERVER_STARTED, status=EventStatus.EVENT_STATUS_ACTIVE)

    def publish_output(self, stream: OutputStream, content: str, source: str = "backend") -> None:
        """Record captured process output when content is nonempty."""
        if content:
            self.record(
                EventType.EVENT_TYPE_OUTPUT,
                data=events_pb2.OutputData(
                    stream=enums.number(events_pb2.OutputStream, stream),
                    source=source,
                    content=content,
                ),
            )

    def record(
        self,
        event_type: EventType.ValueType,
        text: str = "",
        *,
        data: Message | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> events_pb2.RunEvent:
        """Construct and append one server wire event."""
        _require_failure_diagnostic(event_type, fields.get("status"), fields.get("diagnostic"))
        event = messages.make_event(event_type, text, data=data, **fields)
        with self._condition:
            store = self._store
            if store is None:
                self._pending_events.append(event)
                return event
            return self._apply_recorded(store.append(event))

    def append(self, event: events_pb2.RunEvent) -> events_pb2.RunEvent:
        """Append an already-validated wire event.

        Adapters use this path when projecting a timestamped core event. The
        durable store still owns its run identity and sequence assignment.
        """
        _require_failure_diagnostic(
            event.type,
            event.status if event.HasField("status") else None,
            event.diagnostic if event.HasField("diagnostic") else None,
        )
        with self._condition:
            store = self._store
            if store is None:
                self._pending_events.append(event)
                return event
            return self._apply_recorded(store.append(event))

    def record_failure(  # noqa: PLR0913
        self,
        event_type: EventType.ValueType,
        error: BaseException,
        *,
        scope: DiagnosticScope.ValueType,
        operation: str,
        data: Message | None = None,
        data_factory: Callable[[Diagnostic], Message] | None = None,
        text: str | None = None,
        severity: DiagnosticSeverity.ValueType = DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR,
        status: EventStatus.ValueType = EventStatus.EVENT_STATUS_FAILED,
        diagnostic: Diagnostic | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> events_pb2.RunEvent:
        """Record a nonterminal operation failure with stable diagnostics."""
        if event_type not in _NONTERMINAL_FAILURE_EVENTS:
            raise ValueError(f"Cannot record {_name(event_type)} without owning run termination")  # noqa: TRY003
        return self.record_terminal_failure(
            event_type,
            error,
            scope=scope,
            operation=operation,
            data=data,
            data_factory=data_factory,
            text=text,
            severity=severity,
            status=status,
            diagnostic=diagnostic,
            **fields,
        )

    def record_terminal_failure(  # noqa: PLR0913
        self,
        event_type: EventType.ValueType,
        error: BaseException,
        *,
        scope: DiagnosticScope.ValueType,
        operation: str,
        data: Message | None = None,
        data_factory: Callable[[Diagnostic], Message] | None = None,
        text: str | None = None,
        severity: DiagnosticSeverity.ValueType = DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR,
        status: EventStatus.ValueType = EventStatus.EVENT_STATUS_FAILED,
        diagnostic: Diagnostic | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> events_pb2.RunEvent:
        """Record an allowed failure event with stable diagnostics."""
        if event_type not in DIAGNOSTIC_FAILURE_EVENTS:
            raise ValueError(f"{_name(event_type)} is not an operational failure event")  # noqa: TRY003
        diagnostic = diagnostic or self.diagnostic_for(error, scope, operation=operation)
        if diagnostic.severity != severity:
            diagnostic = messages.replace(diagnostic, severity=severity)
        event_data = data_factory(diagnostic) if data_factory is not None else data
        return self.record(
            event_type,
            diagnostic.summary if text is None else text,
            status=status,
            data=event_data,
            diagnostic=diagnostic,
            **fields,
        )

    @contextmanager
    def capture_failure(  # noqa: PLR0913
        self,
        *,
        event_type: EventType.ValueType,
        scope: DiagnosticScope.ValueType,
        operation: str,
        data: Message | None = None,
        data_factory: Callable[[Diagnostic], Message] | None = None,
        text: str | None = None,
        severity: DiagnosticSeverity.ValueType = DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR,
        **fields: Any,  # noqa: ANN401
    ) -> Generator[None]:
        """Record and re-raise an exception from a nonterminal operation."""
        if event_type not in _NONTERMINAL_FAILURE_EVENTS:
            raise ValueError(f"Cannot capture {_name(event_type)} without owning run termination")  # noqa: TRY003
        try:
            yield
        except BaseException as error:
            self.record_failure(
                event_type,
                error,
                scope=scope,
                operation=operation,
                data=data,
                data_factory=data_factory,
                text=text,
                severity=severity,
                **fields,
            )
            raise

    def read(
        self, after_sequence: int = 0, before_sequence: int | None = None
    ) -> list[events_pb2.RunEvent]:
        """Read canonical events within an optional cursor range."""
        with self._condition:
            return self._read_locked(after_sequence, before_sequence)

    def read_history(self) -> list[events_pb2.RunEvent]:
        """Read all canonical events from the durable journal."""
        return self.read()

    def wait_for_events(
        self,
        after_sequence: int,
        timeout: float | None = None,
        before_sequence: int | None = None,
    ) -> list[events_pb2.RunEvent]:
        """Wait for and read events after a cursor."""
        store = self._store
        if store is None:
            return []
        store.wait(after_sequence, timeout)
        with self._condition:
            return self._canonicalize(store.read(after_sequence, before_sequence))

    def wait_for_change(self, after_sequence: int, timeout: float | None = None) -> bool:
        """Wait until the durable sequence advances beyond a cursor."""
        store = self._store
        return False if store is None else store.wait_for_change(after_sequence, timeout)

    @property
    def latest_sequence(self) -> int:
        """Return the latest durable wire-event sequence."""
        with self._condition:
            return self.latest_sequence_locked()

    def latest_sequence_locked(self) -> int:
        """Return the latest sequence while the shared lock is held."""
        store = self._store
        return store.last_sequence if store else 0

    def run_id_locked(self) -> str:
        """Return the durable run id while the shared lock is held."""
        return self._store.run_id if self._store else ""

    def store_id_locked(self) -> str:
        """Return the attached store's identity while the shared lock is held.

        Empty before the first attach, when no sequence space exists yet.
        """
        return self._store.store_id if self._store else ""

    def checkpoint_locked(
        self, after_sequence: int, *, bootstrap_spine: bool = False
    ) -> tuple[int, list[events_pb2.RunEvent]]:
        """Take a watermark-consistent subscription checkpoint."""
        store = self._store
        through_sequence = store.last_sequence if store else 0
        events = store.read(after_sequence) if store else []
        if store is not None and bootstrap_spine and after_sequence > 0:
            events = self._bootstrap_spine_locked(store, after_sequence) + events
        events = self._canonicalize(events)
        return through_sequence, [event for event in events if event.sequence <= through_sequence]

    def clear_diagnostics(self) -> None:
        """Discard cached exception-to-diagnostic identities."""
        with self._condition:
            self._error_diagnostics.clear()

    def diagnostic_for(
        self, error: BaseException, scope: DiagnosticScope.ValueType, *, operation: str
    ) -> Diagnostic:
        """Return one stable diagnostic for an exception chain."""
        key = id(error)
        with self._condition:
            for item in _exception_chain(error):
                cached = self._error_diagnostics.get(id(item))
                if cached is None or cached[0] is not item:
                    continue
                if item is error:
                    return cached[1]
                diagnostic = messages.replace(cached[1], detail=exception_detail(error))
                self._error_diagnostics[key] = (error, diagnostic)
                return diagnostic
        diagnostic = exception_to_diagnostic(
            error,
            scope=scope,
            operation=operation,
            severity=DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR,
            retryability=DiagnosticRetryability.DIAGNOSTIC_RETRYABILITY_UNKNOWN,
        )
        with self._condition:
            self._error_diagnostics[key] = (error, diagnostic)
        return diagnostic

    def _read_locked(
        self, after_sequence: int, before_sequence: int | None
    ) -> list[events_pb2.RunEvent]:
        if self._store is None:
            return []
        return self._canonicalize(self._store.read(after_sequence, before_sequence))

    def _canonicalize(self, events: list[events_pb2.RunEvent]) -> list[events_pb2.RunEvent]:
        return _canonical_execution_events(
            events,
            canonical_lifecycle_ids=self._canonical_execution_ids,
            invocation_lifecycle_ids=self._legacy_invocation_ids,
        )

    def _apply_recorded(self, event: events_pb2.RunEvent) -> events_pb2.RunEvent:
        self._index_execution_identity(event.type, _execution_id(event))
        for listener, _replay_filter in self._listeners:
            listener(event)
        return event

    def _index_stored_history(self, store: EventStore) -> None:
        listener_sequences: set[int] = set()
        for header in store.event_headers():
            self._index_execution_identity(header.type, header.execution_id)
            if any(replay_filter(header) for _listener, replay_filter in self._listeners):
                listener_sequences.add(header.sequence)
        for event in store.read_sequences(sorted(listener_sequences)):
            for listener, replay_filter in self._listeners:
                if replay_filter(header_from_event(event)):
                    listener(event)

    def _index_execution_identity(
        self, event_type: EventType.ValueType, execution_id: str | None
    ) -> None:
        if execution_id is None:
            return
        if event_type in _CANONICAL_LIFECYCLE_EVENTS:
            self._canonical_execution_ids.add(execution_id)
        elif event_type in _LEGACY_LIFECYCLE_EVENTS:
            self._legacy_invocation_ids.add(execution_id)

    @staticmethod
    def _bootstrap_spine_locked(store: EventStore, floor: int) -> list[events_pb2.RunEvent]:
        sequences = [
            header.sequence
            for header in store.event_headers()
            if header.sequence <= floor and header.type in _BOOTSTRAP_SPINE_TYPES
        ]
        return store.read_sequences(sequences)


def _require_failure_diagnostic(
    event_type: EventType.ValueType,
    status: EventStatus.ValueType | None,
    diagnostic: Diagnostic | None,
) -> None:
    """Reject a FAILED operational event that carries no diagnostic.

    Both write paths share this check: ``record`` for server-built events and
    ``append`` for projected core events, so no producer can bypass it.
    """
    if (
        event_type in DIAGNOSTIC_FAILURE_EVENTS
        and status == EventStatus.EVENT_STATUS_FAILED
        and diagnostic is None
    ):
        raise ValueError(f"Failed {_name(event_type)} events must include a diagnostic")  # noqa: TRY003


def _name(event_type: EventType.ValueType) -> str:
    """Return the domain spelling of an event type, for example ``run_failed``."""
    return enums.text(EventType, event_type)


def _execution_id(event: events_pb2.RunEvent) -> str | None:
    return event.execution_id if event.HasField("execution_id") else None


def _exception_chain(error: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and len(chain) < _MAX_EXCEPTION_CHAIN:
        marker = id(current)
        if marker in seen:
            break
        seen.add(marker)
        chain.append(current)
        if current.__cause__ is not None:
            current = current.__cause__
        elif not current.__suppress_context__:
            current = current.__context__
        else:
            current = None
    return chain


def _canonical_execution_events(
    events: list[events_pb2.RunEvent],
    *,
    canonical_lifecycle_ids: set[str] | None = None,
    invocation_lifecycle_ids: set[str] | None = None,
) -> list[events_pb2.RunEvent]:
    """Translate persisted legacy lifecycle events without rewriting their log."""
    if canonical_lifecycle_ids is None:
        canonical_lifecycle_ids = {
            event.execution_id
            for event in events
            if event.type in _CANONICAL_LIFECYCLE_EVENTS and event.HasField("execution_id")
        }
    if invocation_lifecycle_ids is None:
        invocation_lifecycle_ids = {
            event.execution_id
            for event in events
            if event.type in _LEGACY_LIFECYCLE_EVENTS and event.HasField("execution_id")
        }
    canonical: list[events_pb2.RunEvent] = []
    for event in events:
        execution_id = _execution_id(event)
        if execution_id in canonical_lifecycle_ids and event.type in _LEGACY_LIFECYCLE_EVENTS:
            continue
        canonical.append(
            _canonical_event(event, execution_id, canonical_lifecycle_ids, invocation_lifecycle_ids)
        )
    return canonical


def _canonical_event(
    event: events_pb2.RunEvent,
    execution_id: str | None,
    canonical_lifecycle_ids: set[str],
    invocation_lifecycle_ids: set[str],
) -> events_pb2.RunEvent:
    """Return ``event``, or its canonical lifecycle translation when it is legacy."""
    case = event.WhichOneof("data")
    if event.type == EventType.EVENT_TYPE_INVOCATION_STARTED and case == "invocation_started":
        return messages.replace(
            event,
            type=EventType.EVENT_TYPE_AGENT_EXECUTION_STARTED,
            agent_execution_started=_started_data(
                event.agent_kind or "agent",
                _attempt_from_label(event.round_label),
                event.invocation_started.system_prompt,
                event.invocation_started.user_prompt,
            ),
        )
    if event.type == EventType.EVENT_TYPE_INVOCATION_FINISHED and case == "invocation_finished":
        return messages.replace(
            event,
            type=EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED,
            agent_execution_finished=_finished_data(
                event.invocation_finished.result
                if event.invocation_finished.HasField("result")
                else None,
                event.invocation_finished.error
                if event.invocation_finished.HasField("error")
                else None,
            ),
        )
    if (
        event.type in {EventType.EVENT_TYPE_PHASE_STARTED, EventType.EVENT_TYPE_PHASE_FINISHED}
        and case == "phase"
        and execution_id is not None
        and execution_id not in invocation_lifecycle_ids
        and execution_id not in canonical_lifecycle_ids
    ):
        # The phase pair is this execution's only lifecycle record. The
        # translated copy keeps the stored sequence, so it must replace the
        # phase event: emitting both would duplicate one cursor position.
        if event.type == EventType.EVENT_TYPE_PHASE_STARTED:
            return messages.replace(
                event,
                type=EventType.EVENT_TYPE_AGENT_EXECUTION_STARTED,
                agent_execution_started=_started_data(
                    event.phase.phase,
                    event.phase.attempt if event.phase.HasField("attempt") else None,
                ),
            )
        return messages.replace(
            event,
            type=EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED,
            agent_execution_finished=_finished_data(None, event.text or None),
        )
    return event


def _started_data(
    stage: str, attempt: int | None, system_prompt: str = "", user_prompt: str = ""
) -> events_pb2.AgentExecutionStartedData:
    data = events_pb2.AgentExecutionStartedData(
        stage=stage,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        activity=snapshot_pb2.AgentExecutionActivityData(
            mode=snapshot_pb2.ExecutionActivityMode.EXECUTION_ACTIVITY_MODE_THINKING,
            summary=_initial_activity_summary(stage),
        ),
    )
    if attempt is not None:
        data.attempt = attempt
    return data


def _finished_data(
    result: struct_pb2.Value | None, error: str | None
) -> events_pb2.AgentExecutionFinishedData:
    data = events_pb2.AgentExecutionFinishedData()
    if result is not None:
        data.result.CopyFrom(result)
    if error is not None:
        data.error = error
    return data


def _attempt_from_label(round_label: str) -> int | None:
    match = re.search(r"retry-(\d+)", round_label)
    return int(match.group(1)) if match else None


def _initial_activity_summary(kind: str) -> str:
    normalized = kind.lower()
    if "orchestrat" in normalized or "plan" in normalized:
        return "Planning"
    if "implement" in normalized:
        return "Implementing"
    if "judge" in normalized or "review" in normalized:
        return "Reviewing"
    if "profil" in normalized or "benchmark" in normalized:
        return "Profiling"
    if normalized == "chat":
        return "Answering question"
    return f"Running {kind}"
