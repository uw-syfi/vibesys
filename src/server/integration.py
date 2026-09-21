"""Adapter from core run ports to frontend-serving components."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import TypeAdapter

from server.chat.factory import (
    ChatAgentBuilder,
    ExperimentChatFactory,
    build_chat_agent,
)
from server.chat.options import ChatRunSettings
from server.diagnostics import Diagnostic, DiagnosticScope, DiagnosticSeverity
from server.events import (
    AgentExecutionFinishedData,
    EventData,
    EventStatus,
    EventType,
    FrameworkSource,
    FrameworkWarningData,
    InvocationFinishedData,
    PhaseData,
    RunEvent,
)
from server.read_model import RunInspector
from server.run_attachment import AgentSelection, RunAttachment
from server.run_lifecycle import RunTrigger
from vibesys.api import CoreEventType, output_sink
from vs_agent.api import Driver, agent_catalog

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.manager import ChatManager
    from server.controller import ProjectRunState, RunController
    from server.execution import ExecutionTracker
    from server.journal import EventJournal as WireEventJournal
    from vibesys.api import CoreEvent, RunResourceHandoff, RunSession
    from vs_project import Project

_EVENT_DATA_ADAPTER = TypeAdapter(EventData)
_TERMINAL_TRIGGERS: dict[EventType, RunTrigger] = {
    EventType.RUN_FINISHED: RunTrigger.COMPLETED,
    EventType.RUN_FAILED: RunTrigger.FAILED,
}
"""How a core terminal event ends the run the server reports.

The core owns when a run stops; the controller owns the status frontends read.
Settling the controller before the terminal event is appended is what orders
the status change ahead of it in the journal.
"""
_PRESENTATION_EVENTS = frozenset(
    {
        EventType.AGENT_OUTPUT_CHUNK,
        EventType.TOOL_CALL,
        EventType.TOOL_RESULT,
        EventType.TODO_UPDATE,
        EventType.USAGE_UPDATE,
    }
)
_CORE_FAILURE_CONTEXTS: dict[EventType, tuple[DiagnosticScope, DiagnosticSeverity, str]] = {
    EventType.CONFIGURATION_FAILED: (
        DiagnosticScope.CONFIGURATION,
        DiagnosticSeverity.FATAL,
        "Configuration failed",
    ),
    EventType.INVOCATION_FINISHED: (
        DiagnosticScope.INVOCATION,
        DiagnosticSeverity.ERROR,
        "Agent execution failed",
    ),
    EventType.AGENT_EXECUTION_FINISHED: (
        DiagnosticScope.INVOCATION,
        DiagnosticSeverity.ERROR,
        "Agent execution failed",
    ),
    EventType.PHASE_FINISHED: (DiagnosticScope.PHASE, DiagnosticSeverity.ERROR, "Phase failed"),
    EventType.RUN_FAILED: (DiagnosticScope.RUN, DiagnosticSeverity.FATAL, "Run failed"),
    EventType.RUN_INTERRUPTED: (DiagnosticScope.RUN, DiagnosticSeverity.FATAL, "Run interrupted"),
}
"""Scope, severity, and fallback summary per operational failure event.

Keys mirror ``server.journal.DIAGNOSTIC_FAILURE_EVENTS``, the set both journal
write paths enforce a diagnostic for; a coverage test keeps them aligned. Gate
and judge failures stay outside both: they are expected semantic outcomes.
Severity follows the journal's own failure helpers: terminal run events are
fatal, per-invocation and per-phase failures are errors.
"""
_CONTROL_EVENT_TYPES = frozenset(
    {
        CoreEventType.STEER_QUEUED,
        CoreEventType.PAUSE_REQUESTED,
        CoreEventType.RESUMED,
        CoreEventType.STOP_REQUESTED,
        CoreEventType.STEER_CONSUMED,
        CoreEventType.PAUSED,
        CoreEventType.STOPPED,
    }
)
"""Run-control events from `RunControlChannel`, projected onto the controller.

These have no `server.events.EventType` counterpart and never reach the wire
journal directly: `project_event` dispatches them to the matching
`RunController` bookkeeping method instead, which is what still writes the
`CONTROL` wire event and the status transition frontends read. Branching on
these before `EventType(event.type.value)` is what keeps that conversion
total over the events that do have a wire counterpart.
"""
_EXECUTION_FAILURE_EVENTS = frozenset(
    {
        EventType.AGENT_EXECUTION_FINISHED,
        EventType.INVOCATION_FINISHED,
        EventType.PHASE_FINISHED,
    }
)
"""The failure cascade one invocation emits, all stamped with its execution_id.

These fold to a single diagnostic per execution (see ``_event_diagnostic``).
Terminal run failures are excluded: they are run-scoped, carry no execution_id,
and stand on their own.
"""


def _framework_warning_diagnostic(data: FrameworkWarningData) -> Diagnostic:
    """Lift a framework warning into the run-scoped diagnostic surface.

    The full payload still rides on the event's ``data``; the diagnostic is
    the projection frontends already know how to surface.
    """
    return Diagnostic(
        code="framework_warning",
        summary=data.summary,
        detail=data.detail,
        scope=DiagnosticScope.RUN,
        severity=DiagnosticSeverity.WARNING,
        source=data.source_label or data.source.value,
    )


def _core_failure_diagnostic(
    event_type: EventType, text: str, data: EventData | None
) -> Diagnostic:
    """Build a structured diagnostic for a failed core event that lacks one.

    Core events carry unstructured failure facts (event text, a payload error
    string); the projection lifts them into the diagnostic contract so the
    journal invariant holds and frontends need no per-event fallback. Only
    facts the event states are populated: no code beyond the synthesis origin,
    and no hint.
    """
    scope, severity, fallback = _CORE_FAILURE_CONTEXTS[event_type]
    error_text: str | None = None
    if isinstance(data, (AgentExecutionFinishedData, InvocationFinishedData)):
        error_text = data.error
    elif isinstance(data, PhaseData):
        fallback = f"Phase {data.phase} failed"
    summary = text or error_text or fallback
    return Diagnostic(
        code="core_failure",
        summary=summary,
        detail=error_text if error_text is not None and error_text != summary else None,
        scope=scope,
        severity=severity,
        source=FrameworkSource.LOOP.value,
    )


def _core_event_diagnostic(
    event_type: EventType,
    text: str,
    status: EventStatus | None,
    data: EventData | None,
) -> Diagnostic | None:
    """Return the diagnostic a projected core event must carry, if any."""
    if isinstance(data, FrameworkWarningData):
        return _framework_warning_diagnostic(data)
    if event_type in _CORE_FAILURE_CONTEXTS and status is EventStatus.FAILED:
        return _core_failure_diagnostic(event_type, text, data)
    return None


class RunIntegrationAdapter:
    """Implement the neutral core integration port with server components."""

    def __init__(
        self,
        controller: RunController,
        executions: ExecutionTracker,
        journal: WireEventJournal,
        chat: ChatManager,
        *,
        chat_agent_builder: ChatAgentBuilder = build_chat_agent,
    ) -> None:
        """Compose the core port over independently testable server services."""
        self.controller = controller
        self.executions = executions
        self.journal = journal
        self.chat = chat
        self._chat_agent_builder = chat_agent_builder
        self._unsubscribe_output = output_sink().subscribe(self._route_output_event)
        self._chat_factory: ExperimentChatFactory | None = None
        self._detach_run: Callable[[], None] | None = None
        self._closed = False
        self._failure_diagnostics: dict[str, Diagnostic] = {}

    @property
    def project_run(self) -> ProjectRunState | None:
        """Return the attached canonical project run, if available."""
        return self.controller.project_run

    @property
    def current_round(self) -> str | None:
        """Return the current controlled round label."""
        return self.controller.current_round

    @property
    def log_dir(self) -> Path | None:
        """Return the attached wire-journal directory."""
        return self.journal.log_dir

    def status(self) -> str:
        """Return a compact human-readable run status."""
        return self.controller.status()

    def attach(
        self,
        log_dir: Path,
        *,
        project: Project | None = None,
        run_id: str | None = None,
    ) -> None:
        """Attach the wire journal to durable run storage."""
        self._failure_diagnostics.clear()
        self.controller.attach(log_dir, project=project, run_id=run_id)

    def _handle_run_resources(self, session: RunSession, handoff: RunResourceHandoff) -> None:
        """Convert a core resource handoff into durable attach plus experiment chat.

        Registered as this run's sole `RunSession.on_run_resources` listener,
        bound to *session* by `server.runtime.ServerRuntime.drive`. *session*
        is threaded through to `ExperimentChatFactory` so it can open its own
        agent-construction environment through
        `vibesys.api.RunSession.open_agent_environment`. Durable attach runs
        first so the wire journal is attached before any experiment-chat setup
        that might read it.
        """
        self.attach(handoff.log_dir, project=handoff.project, run_id=handoff.run_id)
        attachment = RunAttachment(
            project=handoff.project,
            run_id=handoff.run_id,
            workspace=handoff.workspace,
            log_dir=handoff.log_dir,
            agent_backend=handoff.agent_backend,
            agent_defaults=AgentSelection(
                driver=handoff.driver,
                provider=handoff.provider,
                model=handoff.model,
                role_models=handoff.role_models,
            ),
        )
        self._detach_run = self._attach_run(attachment, session)

    def _attach_run(
        self, attachment: RunAttachment, session: RunSession
    ) -> Callable[[], None] | None:
        """Start the optional experiment-chat surface and return its cleanup callback."""
        defaults = ChatRunSettings(
            driver=attachment.agent_defaults.driver,
            provider=attachment.agent_defaults.provider,
            model=attachment.agent_defaults.model,
            role_models=attachment.agent_defaults.role_models,
        )

        def resolve(
            *, driver: str | None, provider: str | None, model: str | None
        ) -> AgentSelection:
            if attachment.agent_backend != "cli":
                raise ValueError(  # noqa: TRY003
                    "experiment chat threads require the CLI agent backend, "
                    f"but this run uses agent backend {attachment.agent_backend!r}"
                )
            resolved_driver = driver or defaults.driver
            resolved_provider = provider or defaults.provider
            resolved_model = model or defaults.model
            supported = agent_catalog()[Driver(resolved_driver)].providers
            if resolved_provider not in supported:
                raise ValueError(  # noqa: TRY003
                    f"agent driver {resolved_driver!r} does not support provider "
                    f"{resolved_provider!r}; supported providers: {', '.join(supported)}"
                )
            return AgentSelection(
                driver=resolved_driver,
                provider=resolved_provider,
                model=resolved_model,
            )

        previous = self._chat_factory
        if previous is not None:
            previous.close()
        factory = ExperimentChatFactory(
            manager=self.chat,
            controller=self.controller,
            executions=self.executions,
            project=attachment.project,
            run_id=attachment.run_id,
            workspace=attachment.workspace,
            defaults=defaults,
            resolve_selection=resolve,
            session=session,
            attachment=attachment,
            build_agent=self._chat_agent_builder,
            fallback=RunInspector(self).answer,
        )
        self._chat_factory = factory
        try:
            factory.start()
        except Exception as exc:  # optional server feature  # noqa: BLE001
            self.journal.publish_output(
                "stderr",
                f"Experiment chat is unavailable: {type(exc).__name__}: {exc}\n",
                source="experiment-chat",
            )

        def detach() -> None:
            if self._chat_factory is factory:
                self._chat_factory = None
            factory.close()

        return detach

    def close(self) -> None:
        """Release adapter subscriptions and optional server resources."""
        if self._closed:
            return
        self._closed = True
        detach_run, self._detach_run = self._detach_run, None
        if detach_run is not None:
            detach_run()
        self.chat.close_terminal_resource()
        self._unsubscribe_output()

    def record(
        self,
        event_type: EventType,
        text: str = "",
        *,
        data: EventData | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> RunEvent:
        """Record a server-only wire event."""
        return self.journal.record(event_type, text, data=data, **fields)

    def read_events(
        self, after_sequence: int = 0, before_sequence: int | None = None
    ) -> list[RunEvent]:
        """Read canonical wire events within an optional cursor range."""
        return self.journal.read(after_sequence, before_sequence)

    def read_history_events(self) -> list[RunEvent]:
        """Read canonical wire history for inspector queries."""
        return self.journal.read_history()

    def _event_diagnostic(
        self,
        event_type: EventType,
        text: str,
        status: EventStatus | None,
        data: EventData | None,
        execution_id: str | None,
    ) -> Diagnostic | None:
        """Project a core event's diagnostic, folding an execution's failure cascade.

        One failed invocation emits three failure events (agent-execution,
        invocation, phase) that share an ``execution_id``. Each would otherwise
        receive a distinct diagnostic identity, so a frontend that folds by id
        counts one failure as three and lets the phase fallback summary displace
        the invocation's real error. Reuse the first diagnostic built for an
        execution across its cascade so the reports collapse to one carrying the
        error. Run-scoped terminal failures (run failed or interrupted) carry no
        ``execution_id`` and stay independent, as they are distinct faults.
        """
        diagnostic = _core_event_diagnostic(event_type, text, status, data)
        if diagnostic is None or execution_id is None:
            return diagnostic
        if status is not EventStatus.FAILED or event_type not in _EXECUTION_FAILURE_EVENTS:
            return diagnostic
        cached = self._failure_diagnostics.get(execution_id)
        if cached is not None:
            return cached
        self._failure_diagnostics[execution_id] = diagnostic
        return diagnostic

    def project_event(self, event: CoreEvent) -> None:
        """Project one core event onto server tracking state and the wire journal."""
        if event.type in _CONTROL_EVENT_TYPES:
            self._project_control_event(event)
            return
        event_type = EventType(event.type.value)
        data = (
            None
            if event.data is None
            else _EVENT_DATA_ADAPTER.validate_python(event.data.model_dump(mode="python"))
        )
        if event_type is EventType.AGENT_EXECUTION_STARTED:
            self.executions.track_started(event)
        elif event_type is EventType.AGENT_EXECUTION_FINISHED:
            self.executions.discard_finished(event)
            self.controller.reach_invocation_boundary(
                event.agent_kind, event.round_label, event.execution_id
            )
        if event_type in _PRESENTATION_EVENTS:
            # Delivered by `_route_output_event`'s own `output_sink()`
            # subscription instead: both subscriptions see the same events,
            # so handling presentation events here too would double-deliver
            # them.
            return
        terminal_trigger = _TERMINAL_TRIGGERS.get(event_type)
        if terminal_trigger is not None:
            self.controller.settle(terminal_trigger)
        status = EventStatus(event.status.value) if event.status is not None else None
        self.journal.append(
            RunEvent(
                timestamp=event.timestamp,
                type=event_type,
                text=event.text,
                diagnostic=self._event_diagnostic(
                    event_type, event.text, status, data, event.execution_id
                ),
                status=status,
                round_label=event.round_label,
                agent_kind=event.agent_kind,
                execution_id=event.execution_id,
                data=data,
            )
        )

    def _project_control_event(self, event: CoreEvent) -> None:
        """Drive the controller's status/journal bookkeeping from a control event.

        `RunControlChannel` is the sole writer of run-control state; these
        events are its synchronous record of each write. The controller's
        existing status machine and `CONTROL` journal are the wire
        representation of that state, so this only calls its existing
        bookkeeping methods -- it does not append a second wire event.
        """
        if event.type is CoreEventType.STEER_QUEUED:
            self.controller.steer(event.text)
        elif event.type is CoreEventType.PAUSE_REQUESTED:
            self.controller.pause_after_call()
        elif event.type is CoreEventType.RESUMED:
            self.controller.resume()
        elif event.type is CoreEventType.STOP_REQUESTED:
            self.controller.stop_after_call()
        elif event.type is CoreEventType.STEER_CONSUMED:
            self.controller.record_steer_consumed(
                agent_kind=event.agent_kind,
                round_label=event.round_label,
                execution_id=event.execution_id,
            )
        elif event.type is CoreEventType.PAUSED:
            self.controller.land_pause_at_boundary()
        elif event.type is CoreEventType.STOPPED:
            self.controller.land_stop_at_boundary()

    def _route_output_event(self, event: CoreEvent) -> None:
        """Publish a presentation event straight from `output_sink()`.

        This is now the sole delivery path for `_PRESENTATION_EVENTS`, for
        both the main run and experiment chat alike: `project_event` early
        returns for these event types (see its own docstring note), and
        chat's presentation never needed the full core-event pipeline in the
        first place (`ExecutionTracker.presentation_scope` is its real entry
        point).
        """
        event_type = EventType(event.type.value)
        if event_type not in _PRESENTATION_EVENTS or event.data is None:
            return
        data = _EVENT_DATA_ADAPTER.validate_python(event.data.model_dump(mode="python"))
        self.executions.publish_presentation(
            event_type,
            data,
            agent_kind=event.agent_kind,
            round_label=event.round_label,
            invocation_id=event.execution_id,
        )
