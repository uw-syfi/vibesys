"""Durable append/replay journal for server wire events."""

from __future__ import annotations

import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path  # noqa: TC003
from typing import TYPE_CHECKING, Any

from server.diagnostics import (
    Diagnostic,
    DiagnosticRetryability,
    DiagnosticScope,
    DiagnosticSeverity,
    exception_detail,
    exception_to_diagnostic,
)
from server.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    EventData,
    EventHeader,
    EventStatus,
    EventStore,
    EventType,
    InvocationFinishedData,
    InvocationStartedData,
    OutputData,
    OutputStream,
    PhaseData,
    RunEvent,
    make_event,
)

if TYPE_CHECKING:
    import threading

_MAX_EXCEPTION_CHAIN = 8
DIAGNOSTIC_FAILURE_EVENTS = frozenset(
    {
        EventType.CONFIGURATION_FAILED,
        EventType.INVOCATION_FINISHED,
        EventType.AGENT_EXECUTION_FINISHED,
        EventType.PHASE_FINISHED,
        EventType.RUN_FAILED,
        EventType.RUN_INTERRUPTED,
    }
)
"""Operational failure events that must carry a diagnostic when FAILED.

Gate and judge outcomes are deliberately absent: a failed gate or judge
verdict is an expected semantic result, not an operational fault.
"""
_NONTERMINAL_FAILURE_EVENTS = frozenset(
    {
        EventType.INVOCATION_FINISHED,
        EventType.AGENT_EXECUTION_FINISHED,
        EventType.PHASE_FINISHED,
    }
)
_CANONICAL_LIFECYCLE_EVENTS = frozenset(
    {EventType.AGENT_EXECUTION_STARTED, EventType.AGENT_EXECUTION_FINISHED}
)
_LEGACY_LIFECYCLE_EVENTS = frozenset({EventType.INVOCATION_STARTED, EventType.INVOCATION_FINISHED})
_BOOTSTRAP_SPINE_TYPES = frozenset(
    {
        EventType.RUN_STARTED,
        EventType.RUN_STATUS_CHANGED,
        EventType.RUN_FINISHED,
        EventType.RUN_FAILED,
        EventType.RUN_INTERRUPTED,
        EventType.CONFIGURATION_FAILED,
        EventType.ROUND_FINISHED,
        EventType.EXPERIMENTS_CHANGED,
        EventType.CHAT_THREAD_CREATED,
    }
)

EventListener = Callable[[RunEvent], None]
HeaderFilter = Callable[[EventHeader], bool]


class EventJournal:
    """Own event serialization, replay compatibility, and failure identity."""

    def __init__(self, condition: threading.Condition) -> None:
        """Initialize journal state over the shared server condition."""
        self._condition = condition
        self._store: EventStore | None = None
        self._pending_events: list[RunEvent] = []
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
            for event in pending:
                self._apply_recorded(durable.append(event))
            self._store = durable
            self.log_dir = log_dir
            started_fresh = previous is None
            if previous is not None:
                previous.notify_change()
        if started_fresh:
            self.record(EventType.SERVER_STARTED, status=EventStatus.ACTIVE)

    def publish_output(self, stream: OutputStream, content: str, source: str = "backend") -> None:
        """Record captured process output when content is nonempty."""
        if content:
            self.record(
                EventType.OUTPUT,
                data=OutputData(stream=stream, source=source, content=content),
            )

    def record(
        self,
        event_type: EventType,
        text: str = "",
        *,
        data: EventData | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> RunEvent:
        """Construct and append one server wire event."""
        _require_failure_diagnostic(event_type, fields.get("status"), fields.get("diagnostic"))
        event = make_event(event_type, text, data=data, **fields)
        with self._condition:
            store = self._store
            if store is None:
                self._pending_events.append(event)
                return event
            return self._apply_recorded(store.append(event))

    def append(self, event: RunEvent) -> RunEvent:
        """Append an already-validated wire event.

        Adapters use this path when projecting a timestamped core event. The
        durable store still owns its run identity and sequence assignment.
        """
        _require_failure_diagnostic(event.type, event.status, event.diagnostic)
        with self._condition:
            store = self._store
            if store is None:
                self._pending_events.append(event)
                return event
            return self._apply_recorded(store.append(event))

    def record_failure(  # noqa: PLR0913
        self,
        event_type: EventType,
        error: BaseException,
        *,
        scope: DiagnosticScope,
        operation: str,
        data: EventData | None = None,
        data_factory: Callable[[Diagnostic], EventData] | None = None,
        text: str | None = None,
        severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
        status: EventStatus = EventStatus.FAILED,
        diagnostic: Diagnostic | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> RunEvent:
        """Record a nonterminal operation failure with stable diagnostics."""
        if event_type not in _NONTERMINAL_FAILURE_EVENTS:
            raise ValueError(f"Cannot record {event_type.value} without owning run termination")  # noqa: TRY003
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
        event_type: EventType,
        error: BaseException,
        *,
        scope: DiagnosticScope,
        operation: str,
        data: EventData | None = None,
        data_factory: Callable[[Diagnostic], EventData] | None = None,
        text: str | None = None,
        severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
        status: EventStatus = EventStatus.FAILED,
        diagnostic: Diagnostic | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> RunEvent:
        """Record an allowed failure event with stable diagnostics."""
        if event_type not in DIAGNOSTIC_FAILURE_EVENTS:
            raise ValueError(f"{event_type.value} is not an operational failure event")  # noqa: TRY003
        diagnostic = diagnostic or self.diagnostic_for(error, scope, operation=operation)
        if diagnostic.severity is not severity:
            diagnostic = diagnostic.model_copy(update={"severity": severity})
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
        event_type: EventType,
        scope: DiagnosticScope,
        operation: str,
        data: EventData | None = None,
        data_factory: Callable[[Diagnostic], EventData] | None = None,
        text: str | None = None,
        severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
        **fields: Any,  # noqa: ANN401
    ) -> Generator[None]:
        """Record and re-raise an exception from a nonterminal operation."""
        if event_type not in _NONTERMINAL_FAILURE_EVENTS:
            raise ValueError(f"Cannot capture {event_type.value} without owning run termination")  # noqa: TRY003
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

    def read(self, after_sequence: int = 0, before_sequence: int | None = None) -> list[RunEvent]:
        """Read canonical events within an optional cursor range."""
        with self._condition:
            return self._read_locked(after_sequence, before_sequence)

    def read_history(self) -> list[RunEvent]:
        """Read all canonical events from the durable journal."""
        return self.read()

    def wait_for_events(
        self,
        after_sequence: int,
        timeout: float | None = None,
        before_sequence: int | None = None,
    ) -> list[RunEvent]:
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

    def checkpoint_locked(
        self, after_sequence: int, *, bootstrap_spine: bool = False
    ) -> tuple[int, list[RunEvent]]:
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
        self, error: BaseException, scope: DiagnosticScope, *, operation: str
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
                diagnostic = cached[1].model_copy(update={"detail": exception_detail(error)})
                self._error_diagnostics[key] = (error, diagnostic)
                return diagnostic
        diagnostic = exception_to_diagnostic(
            error,
            scope=scope,
            operation=operation,
            severity=DiagnosticSeverity.ERROR,
            retryability=DiagnosticRetryability.UNKNOWN,
        )
        with self._condition:
            self._error_diagnostics[key] = (error, diagnostic)
        return diagnostic

    def _read_locked(self, after_sequence: int, before_sequence: int | None) -> list[RunEvent]:
        if self._store is None:
            return []
        return self._canonicalize(self._store.read(after_sequence, before_sequence))

    def _canonicalize(self, events: list[RunEvent]) -> list[RunEvent]:
        return _canonical_execution_events(
            events,
            canonical_lifecycle_ids=self._canonical_execution_ids,
            invocation_lifecycle_ids=self._legacy_invocation_ids,
        )

    def _apply_recorded(self, event: RunEvent) -> RunEvent:
        self._index_execution_identity(event.type, event.execution_id)
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
                if replay_filter(_header_from_event(event)):
                    listener(event)

    def _index_execution_identity(self, event_type: EventType, execution_id: str | None) -> None:
        if execution_id is None:
            return
        if event_type in _CANONICAL_LIFECYCLE_EVENTS:
            self._canonical_execution_ids.add(execution_id)
        elif event_type in _LEGACY_LIFECYCLE_EVENTS:
            self._legacy_invocation_ids.add(execution_id)

    @staticmethod
    def _bootstrap_spine_locked(store: EventStore, floor: int) -> list[RunEvent]:
        sequences = [
            header.sequence
            for header in store.event_headers()
            if header.sequence <= floor and header.type in _BOOTSTRAP_SPINE_TYPES
        ]
        return store.read_sequences(sequences)


def _require_failure_diagnostic(
    event_type: EventType,
    status: object,
    diagnostic: Diagnostic | None,
) -> None:
    """Reject a FAILED operational event that carries no diagnostic.

    Both write paths share this check: ``record`` for server-built events and
    ``append`` for projected core events, so no producer can bypass it.
    """
    if (
        event_type in DIAGNOSTIC_FAILURE_EVENTS
        and status in {EventStatus.FAILED, EventStatus.FAILED.value}
        and diagnostic is None
    ):
        raise ValueError(f"Failed {event_type.value} events must include a diagnostic")  # noqa: TRY003


def _header_from_event(event: RunEvent) -> EventHeader:
    return EventHeader(
        sequence=event.sequence,
        type=event.type,
        execution_id=event.execution_id,
        chat_thread_id=event.chat_thread_id,
    )


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
    events: list[RunEvent],
    *,
    canonical_lifecycle_ids: set[str] | None = None,
    invocation_lifecycle_ids: set[str] | None = None,
) -> list[RunEvent]:
    """Translate persisted legacy lifecycle events without rewriting their log."""
    if canonical_lifecycle_ids is None:
        canonical_lifecycle_ids = {
            event.execution_id
            for event in events
            if event.type in {EventType.AGENT_EXECUTION_STARTED, EventType.AGENT_EXECUTION_FINISHED}
            and event.execution_id is not None
        }
    if invocation_lifecycle_ids is None:
        invocation_lifecycle_ids = {
            event.execution_id
            for event in events
            if event.type in {EventType.INVOCATION_STARTED, EventType.INVOCATION_FINISHED}
            and event.execution_id is not None
        }
    canonical: list[RunEvent] = []
    for event in events:
        if event.execution_id in canonical_lifecycle_ids and event.type in {
            EventType.INVOCATION_STARTED,
            EventType.INVOCATION_FINISHED,
        }:
            continue
        if event.type is EventType.INVOCATION_STARTED and isinstance(
            event.data, InvocationStartedData
        ):
            canonical.append(
                event.model_copy(
                    update={
                        "type": EventType.AGENT_EXECUTION_STARTED,
                        "data": AgentExecutionStartedData(
                            stage=event.agent_kind or "agent",
                            attempt=_attempt_from_label(event.round_label or ""),
                            system_prompt=event.data.system_prompt,
                            user_prompt=event.data.user_prompt,
                            activity=AgentExecutionActivityData(
                                mode="thinking",
                                summary=_initial_activity_summary(event.agent_kind or "agent"),
                            ),
                        ),
                    }
                )
            )
            continue
        if event.type is EventType.INVOCATION_FINISHED and isinstance(
            event.data, InvocationFinishedData
        ):
            canonical.append(
                event.model_copy(
                    update={
                        "type": EventType.AGENT_EXECUTION_FINISHED,
                        "data": AgentExecutionFinishedData(
                            result=event.data.result, error=event.data.error
                        ),
                    }
                )
            )
            continue
        if event.type in {EventType.PHASE_STARTED, EventType.PHASE_FINISHED} and isinstance(
            event.data, PhaseData
        ):
            if (
                event.execution_id is not None
                and event.execution_id not in invocation_lifecycle_ids
                and event.execution_id not in canonical_lifecycle_ids
                and event.type is EventType.PHASE_STARTED
            ):
                canonical.append(
                    event.model_copy(
                        update={
                            "type": EventType.AGENT_EXECUTION_STARTED,
                            "data": AgentExecutionStartedData(
                                stage=event.data.phase,
                                attempt=event.data.attempt,
                                activity=AgentExecutionActivityData(
                                    mode="thinking",
                                    summary=_initial_activity_summary(event.data.phase),
                                ),
                            ),
                        }
                    )
                )
            elif (
                event.execution_id is not None
                and event.execution_id not in invocation_lifecycle_ids
                and event.execution_id not in canonical_lifecycle_ids
            ):
                canonical.append(
                    event.model_copy(
                        update={
                            "type": EventType.AGENT_EXECUTION_FINISHED,
                            "data": AgentExecutionFinishedData(error=event.text or None),
                        }
                    )
                )
            canonical.append(event)
            continue
        canonical.append(event)
    return canonical


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
