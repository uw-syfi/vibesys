"""Durable append/replay journal for server wire events."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING, cast

from server.diagnostics import (
    Diagnostic,
    DiagnosticRetryability,
    DiagnosticScope,
    DiagnosticSeverity,
    exception_detail,
    exception_to_diagnostic,
)
from server.events import (
    READER_APPENDABLE_EVENTS,
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
from server.host_api import RUN_EVENTS_FILE
from server.trace import TraceProjection
from vs_sim.api import utc_datetime

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import Clock, Condition, Threads

_MAX_EXCEPTION_CHAIN = 8
_LOG = logging.getLogger(__name__)
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
        EventType.ITERATIONS_CHANGED,
        EventType.CHAT_THREAD_CREATED,
    }
)

EventListener = Callable[[RunEvent], None]
HeaderFilter = Callable[[EventHeader], bool]
ListenerErrorSink = Callable[[Exception, EventHeader], None]
ListenerFailures = list[tuple[Exception, EventHeader]]


def _log_listener_error(error: Exception, header: EventHeader) -> None:
    """Report one failed post-commit observer without changing commit outcome."""
    _LOG.error(
        "journal observer failed while applying committed %s event at sequence %s",
        header.type.value,
        header.sequence,
        exc_info=error,
    )


class _RunIdentityError(ValueError):
    """A writable journal was asked to name no run, or to change its run."""

    @classmethod
    def unnamed(cls) -> _RunIdentityError:
        return cls("A writable journal must name its run")

    @classmethod
    def changed(cls, attached: str, requested: str) -> _RunIdentityError:
        return cls(f"Journal of run {attached!r} cannot continue as run {requested!r}")


def _writable_run_id(attached: str | None, requested: str | None) -> str:
    if not requested:
        raise _RunIdentityError.unnamed()
    if attached is not None and requested != attached:
        raise _RunIdentityError.changed(attached, requested)
    return requested


class _ReadOnlyRunIdentityError(ValueError):
    """A durable replay log cannot establish one requested identity."""

    @classmethod
    def empty_explicit(cls) -> _ReadOnlyRunIdentityError:
        return cls("An explicit read-only run id must be non-empty")

    @classmethod
    def mixed(cls, values: str) -> _ReadOnlyRunIdentityError:
        return cls(f"Read-only journal contains multiple run ids: {values}")

    @classmethod
    def mismatch(cls, explicit: str, recorded: str) -> _ReadOnlyRunIdentityError:
        return cls(
            f"Explicit read-only run id {explicit!r} does not match recorded id {recorded!r}"
        )

    @classmethod
    def missing(cls) -> _ReadOnlyRunIdentityError:
        return cls("Read-only journal has no recorded run id; pass one explicitly")


def _read_only_run_id(headers: list[EventHeader], explicit: str | None) -> str:
    """Resolve one durable replay identity from indexed event headers."""
    if explicit == "":
        raise _ReadOnlyRunIdentityError.empty_explicit()
    recorded = {header.run_id for header in headers if header.run_id != ""}
    if len(recorded) > 1:
        values = ", ".join(sorted(recorded))
        raise _ReadOnlyRunIdentityError.mixed(values)
    journal_id = next(iter(recorded), None)
    if explicit is not None:
        if journal_id is not None and explicit != journal_id:
            raise _ReadOnlyRunIdentityError.mismatch(explicit, journal_id)
        return explicit
    if journal_id is None:
        raise _ReadOnlyRunIdentityError.missing()
    return journal_id


class WireJournal:
    """Own event serialization, replay compatibility, and failure identity."""

    def __init__(
        self,
        condition: Condition,
        *,
        clock: Clock,
        threads: Threads,
        listener_errors: ListenerErrorSink = _log_listener_error,
    ) -> None:
        """Journal events and route post-commit observer failures to their explicit owner."""
        self._condition = condition
        self._clock = clock
        self._threads = threads
        self._listener_errors = listener_errors
        self._listener_error_lock = threads.rlock()
        self._reporting_listener_error = False
        self._store: EventStore | None = None
        self._pending_events: list[RunEvent] = []
        self._canonical_execution_ids: set[str] = set()
        self._legacy_invocation_ids: set[str] = set()
        self._error_diagnostics: dict[int, tuple[BaseException, Diagnostic]] = {}
        self.trace = TraceProjection()
        """The run's spans, folded from its span events (live and replayed). The journal stamps
        each turn's events with the turn's span from it."""
        self._listeners: list[tuple[EventListener, HeaderFilter]] = [
            (self.trace.observe, TraceProjection.replay_filter)
        ]
        self.log_dir: Path | None = None
        self._read_only = False

    def add_listener(self, listener: EventListener, *, replay_filter: HeaderFilter) -> None:
        """Register a live append reducer and its selective replay filter."""
        with self._condition:
            self._listeners.append((listener, replay_filter))

    def attach(  # noqa: C901  # lint-waiver: LW-101046 [C901]; attachment handles store replacement, replay continuity, and read-only validation as one atomic operation
        self, log_dir: Path, *, run_id: str | None = None, read_only: bool = False
    ) -> None:
        """Attach the journal to a durable run event file.

        A writable journal names its run from the first event, and keeps that
        name when it moves to another file: ``run_id`` is required and must
        match the run already attached. A read-only journal takes its name
        from the recorded events, checked against ``run_id`` when given.
        """
        if read_only:
            if not log_dir.is_dir():
                raise FileNotFoundError(log_dir)
        else:
            log_dir.mkdir(parents=True, exist_ok=True)
        events_path = log_dir / RUN_EVENTS_FILE
        if read_only and not events_path.is_file():
            raise FileNotFoundError(events_path)
        listener_failures: ListenerFailures = []
        started_fresh = False
        try:
            with self._condition:
                previous = self._store
                if previous is not None and previous.path == events_path:
                    previous.run_id = (
                        _read_only_run_id(previous.event_headers(), run_id)
                        if read_only
                        else _writable_run_id(previous.run_id, run_id)
                    )
                    self.log_dir = log_dir
                    self._read_only = read_only
                    return
                durable = (
                    EventStore(
                        events_path,
                        run_id=_writable_run_id(previous.run_id if previous else None, run_id),
                        threads=self._threads,
                    )
                    if not read_only
                    else EventStore(
                        events_path,
                        run_id="",
                        read_only=True,
                        appendable=READER_APPENDABLE_EVENTS,
                        threads=self._threads,
                    )
                )
                if read_only:
                    durable.run_id = _read_only_run_id(durable.event_headers(), run_id)
                self._index_stored_history(durable, listener_failures)
                pending = previous.read() if previous is not None else self._pending_events
                if read_only and pending:
                    raise RuntimeError("Cannot replace a read-only journal with pending events")  # noqa: TRY003  # lint-waiver: LW-101047 [TRY003]; reject an unsafe store transition instead of silently dropping pending events
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
                    self._apply_recorded(
                        durable.append(self._with_turn_span(event)), listener_failures
                    )
                self._store = durable
                self.log_dir = log_dir
                started_fresh = previous is None and not read_only
                self._read_only = read_only
                if previous is not None:
                    previous.notify_change()
                self._condition.notify_all()
        finally:
            self._report_listener_failures(listener_failures)
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
        **fields: object,
    ) -> RunEvent:
        """Construct and append one server wire event."""
        _require_failure_diagnostic(event_type, fields.get("status"), fields.get("diagnostic"))
        event = make_event(
            event_type, text, timestamp=utc_datetime(self._clock), data=data, **fields
        )
        listener_failures: ListenerFailures = []
        try:
            with self._condition:
                if self._read_only:
                    if event.type not in READER_APPENDABLE_EVENTS or self._store is None:
                        return event
                else:
                    event = self._with_turn_span(event)
                store = self._store
                if store is None:
                    self._pending_events.append(event)
                    return event
                return self._apply_recorded(store.append(event), listener_failures)
        finally:
            self._report_listener_failures(listener_failures)

    def append(self, event: RunEvent) -> RunEvent:
        """Append an already-validated wire event.

        Adapters use this path when projecting a timestamped core event. The
        durable store still owns its run identity and sequence assignment.
        """
        _require_failure_diagnostic(event.type, event.status, event.diagnostic)
        listener_failures: ListenerFailures = []
        try:
            with self._condition:
                if self._read_only:
                    if event.type not in READER_APPENDABLE_EVENTS or self._store is None:
                        return event
                else:
                    event = self._with_turn_span(event)
                store = self._store
                if store is None:
                    self._pending_events.append(event)
                    return event
                return self._apply_recorded(store.append(event), listener_failures)
        finally:
            self._report_listener_failures(listener_failures)

    def record_failure(  # noqa: PLR0913  # lint-waiver: LW-011108 [PLR0913]; The nonterminal API accepts event-specific payload/text and optional prebuilt diagnostics while deriving defaults from error, scope, and operation.
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
        **fields: object,
    ) -> RunEvent:
        """Record a nonterminal operation failure with stable diagnostics."""
        if event_type not in _NONTERMINAL_FAILURE_EVENTS:
            message = f"Cannot record {event_type.value} without owning run termination"
            raise ValueError(message)
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

    def record_terminal_failure(  # noqa: PLR0913  # lint-waiver: LW-011109 [PLR0913]; Terminal event callers choose event-specific payload/text, status/severity, or a diagnostic override; these are distinct wire-event fields.
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
        **fields: object,
    ) -> RunEvent:
        """Record an allowed failure event with stable diagnostics."""
        if event_type not in DIAGNOSTIC_FAILURE_EVENTS:
            message = f"{event_type.value} is not an operational failure event"
            raise ValueError(message)
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
    def capture_failure(  # noqa: PLR0913  # lint-waiver: LW-011110 [PLR0913]; This context manager must carry the same event-specific payload, diagnostic factory, text, severity, and event fields as record_failure while re-raising.
        self,
        *,
        event_type: EventType,
        scope: DiagnosticScope,
        operation: str,
        data: EventData | None = None,
        data_factory: Callable[[Diagnostic], EventData] | None = None,
        text: str | None = None,
        severity: DiagnosticSeverity = DiagnosticSeverity.ERROR,
        **fields: object,
    ) -> Generator[None]:
        """Record and re-raise an exception from a nonterminal operation."""
        if event_type not in _NONTERMINAL_FAILURE_EVENTS:
            message = f"Cannot capture {event_type.value} without owning run termination"
            raise ValueError(message)
        try:
            yield
        except BaseException as error:
            status = cast("EventStatus", fields.pop("status", EventStatus.FAILED))
            diagnostic = cast("Diagnostic | None", fields.pop("diagnostic", None))
            self.record_failure(
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

    @property
    def read_only(self) -> bool:
        """Whether this journal accepts no appends."""
        with self._condition:
            return self._read_only

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

    def read_fenced_locked(
        self, expected_store_id: str, after_sequence: int, before_sequence: int | None
    ) -> list[RunEvent] | None:
        """Read only if the shared-lock store still has the expected identity."""
        if self.store_id_locked() != expected_store_id:
            return None
        return self._read_locked(after_sequence, before_sequence)

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

    def _with_turn_span(self, event: RunEvent) -> RunEvent:
        """``event`` naming the agent-turn span its execution ran as, when the trace has it."""
        if event.span_id is not None or event.execution_id is None:
            return event
        span = self.trace.turn_span(event.execution_id)
        return event if span is None else event.model_copy(update={"span_id": span})

    def _apply_recorded(self, event: RunEvent, listener_failures: ListenerFailures) -> RunEvent:
        self._index_execution_identity(event.type, event.execution_id)
        try:
            self._notify_listeners(
                event,
                [listener for listener, _filter in self._listeners],
                listener_failures,
            )
        finally:
            # The durable append is the commit boundary. Even a control-flow
            # exception from an observer cannot hide that commit from waiters.
            self._condition.notify_all()
        return event

    def _index_stored_history(self, store: EventStore, listener_failures: ListenerFailures) -> None:
        listeners_by_sequence: dict[int, list[EventListener]] = {}
        for header in store.event_headers():
            self._index_execution_identity(header.type, header.execution_id)
            selected: list[EventListener] = []
            for listener, replay_filter in self._listeners:
                try:
                    interested = replay_filter(header)
                except Exception as error:  # noqa: BLE001  # lint-waiver: LW-101120 [BLE001]; replay filters are extension observers after durable commit, so every unexpected failure is reported without starving independent projections; enumerating provider exception types would make the journal depend on adapters.
                    listener_failures.append((error, header))
                    continue
                if interested:
                    selected.append(listener)
            if selected:
                listeners_by_sequence[header.sequence] = selected
        for event in store.read_sequences(sorted(listeners_by_sequence)):
            self._notify_listeners(event, listeners_by_sequence[event.sequence], listener_failures)

    def _notify_listeners(
        self,
        event: RunEvent,
        listeners: list[EventListener],
        listener_failures: ListenerFailures,
    ) -> None:
        """Attempt every observer of one committed event exactly once."""
        header = _header_from_event(event)
        for listener in listeners:
            try:
                listener(event)
            except Exception as error:  # noqa: BLE001  # lint-waiver: LW-101121 [BLE001]; listeners are arbitrary projection adapters after durable commit, so all failures must be isolated and routed without making the append appear uncommitted; narrowing by adapter would invert the dependency.
                listener_failures.append((error, header))

    def _report_listener_failures(self, listener_failures: ListenerFailures) -> None:
        """Report observer faults after releasing the journal's commit lock."""
        for error, header in listener_failures:
            self._report_listener_error(error, header)

    def _report_listener_error(self, error: Exception, header: EventHeader) -> None:
        """Invoke the failure owner without letting its own fault rewrite history."""
        with self._listener_error_lock:
            if self._reporting_listener_error:
                _log_listener_error(error, header)
                return
            self._reporting_listener_error = True
            try:
                self._listener_errors(error, header)
            except Exception as sink_error:  # noqa: BLE001  # lint-waiver: LW-101122 [BLE001]; the diagnostic sink is the final extension boundary and cannot be allowed to escape after commit; narrower catches would let unknown sinks violate append authority.
                _LOG.error(
                    "journal observer error sink failed for sequence %s",
                    header.sequence,
                    exc_info=sink_error,
                )
                _log_listener_error(error, header)
            finally:
                self._reporting_listener_error = False

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
    diagnostic: object,
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
        message = f"Failed {event_type.value} events must include a diagnostic"
        raise ValueError(message)


def _header_from_event(event: RunEvent) -> EventHeader:
    return EventHeader(
        sequence=event.sequence,
        type=event.type,
        execution_id=event.execution_id,
        chat_thread_id=event.chat_thread_id,
        run_id=event.run_id,
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
        if (
            event.type in {EventType.PHASE_STARTED, EventType.PHASE_FINISHED}
            and isinstance(event.data, PhaseData)
            and event.execution_id is not None
            and event.execution_id not in invocation_lifecycle_ids
            and event.execution_id not in canonical_lifecycle_ids
        ):
            # The phase pair is this execution's only lifecycle record. The
            # translated copy keeps the stored sequence, so it must replace the
            # phase event: emitting both would duplicate one cursor position.
            if event.type is EventType.PHASE_STARTED:
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
            else:
                canonical.append(
                    event.model_copy(
                        update={
                            "type": EventType.AGENT_EXECUTION_FINISHED,
                            "data": AgentExecutionFinishedData(error=event.text or None),
                        }
                    )
                )
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
