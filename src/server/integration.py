"""Adapter from core run ports to frontend-serving components."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from server.chat.factory import (
    ChatAgentBuilder,
    ExperimentChatFactory,
    build_chat_agent,
)
from server.chat.options import ChatRunSettings
from server.diagnostics import (
    DiagnosticScope,
    DiagnosticSeverity,
    make_diagnostic,
)
from server.read_model import RunInspector
from server.run_lifecycle import RunTrigger
from server.wire import codec, enums, messages, upgrade
from server.wire.v2 import events_pb2
from vibesys.agents.factory import supported_cli_providers
from vibesys.render.sink import output_sink
from vibesys.run.event_journal import EventJournal as CoreEventJournal
from vibesys.run.integration import (
    AgentSelection,
    ExecutionHandle,
    InvocationLifecycle,
    RunAttachment,
)

if TYPE_CHECKING:
    from collections.abc import Generator

    from google.protobuf.message import Message

    from server.chat.manager import ChatManager
    from server.controller import ProjectRunState, RunController
    from server.execution import ExecutionTracker
    from server.journal import EventJournal as WireEventJournal
    from server.wire.v2.common_pb2 import Diagnostic
    from vibesys.events import CoreEvent
    from vs_project import Project

CommittedStateListener = Callable[[str, Path, str, BaseModel, tuple[str, ...] | None], None]

EventType = events_pb2.EventType
EventStatus = events_pb2.EventStatus
_PAYLOAD_TYPES: dict[str, type[Message]] = {
    field.name: type(getattr(events_pb2.RunEvent(), field.name))
    for field in events_pb2.RunEvent.DESCRIPTOR.oneofs_by_name["data"].fields
}
_TERMINAL_TRIGGERS: dict[EventType, RunTrigger] = {
    EventType.EVENT_TYPE_RUN_FINISHED: RunTrigger.COMPLETED,
    EventType.EVENT_TYPE_RUN_FAILED: RunTrigger.FAILED,
}
"""How a core terminal event ends the run the server reports.

The core owns when a run stops; the controller owns the status frontends read.
Settling the controller before the terminal event is appended is what orders
the status change ahead of it in the journal.
"""
_PRESENTATION_EVENTS = frozenset(
    {
        EventType.EVENT_TYPE_AGENT_OUTPUT_CHUNK,
        EventType.EVENT_TYPE_TOOL_CALL,
        EventType.EVENT_TYPE_TOOL_RESULT,
        EventType.EVENT_TYPE_TODO_UPDATE,
        EventType.EVENT_TYPE_USAGE_UPDATE,
    }
)
_CORE_FAILURE_CONTEXTS: dict[EventType, tuple[DiagnosticScope, DiagnosticSeverity, str]] = {
    EventType.EVENT_TYPE_CONFIGURATION_FAILED: (
        DiagnosticScope.DIAGNOSTIC_SCOPE_CONFIGURATION,
        DiagnosticSeverity.DIAGNOSTIC_SEVERITY_FATAL,
        "Configuration failed",
    ),
    EventType.EVENT_TYPE_INVOCATION_FINISHED: (
        DiagnosticScope.DIAGNOSTIC_SCOPE_INVOCATION,
        DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR,
        "Agent execution failed",
    ),
    EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED: (
        DiagnosticScope.DIAGNOSTIC_SCOPE_INVOCATION,
        DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR,
        "Agent execution failed",
    ),
    EventType.EVENT_TYPE_PHASE_FINISHED: (
        DiagnosticScope.DIAGNOSTIC_SCOPE_PHASE,
        DiagnosticSeverity.DIAGNOSTIC_SEVERITY_ERROR,
        "Phase failed",
    ),
    EventType.EVENT_TYPE_RUN_FAILED: (
        DiagnosticScope.DIAGNOSTIC_SCOPE_RUN,
        DiagnosticSeverity.DIAGNOSTIC_SEVERITY_FATAL,
        "Run failed",
    ),
    EventType.EVENT_TYPE_RUN_INTERRUPTED: (
        DiagnosticScope.DIAGNOSTIC_SCOPE_RUN,
        DiagnosticSeverity.DIAGNOSTIC_SEVERITY_FATAL,
        "Run interrupted",
    ),
}
"""Scope, severity, and fallback summary per operational failure event.

Keys mirror ``server.journal.DIAGNOSTIC_FAILURE_EVENTS``, the set both journal
write paths enforce a diagnostic for; a coverage test keeps them aligned. Gate
and judge failures stay outside both: they are expected semantic outcomes.
Severity follows the journal's own failure helpers: terminal run events are
fatal, per-invocation and per-phase failures are errors.
"""
_EXECUTION_FAILURE_EVENTS = frozenset(
    {
        EventType.EVENT_TYPE_AGENT_EXECUTION_FINISHED,
        EventType.EVENT_TYPE_INVOCATION_FINISHED,
        EventType.EVENT_TYPE_PHASE_FINISHED,
    }
)
"""The failure cascade one invocation emits, all stamped with its execution_id.

These fold to a single diagnostic per execution (see ``_event_diagnostic``).
Terminal run failures are excluded: they are run-scoped, carry no execution_id,
and stand on their own.
"""


def _framework_warning_diagnostic(data: events_pb2.FrameworkWarningData) -> Diagnostic:
    """Lift a framework warning into the run-scoped diagnostic surface.

    The full payload still rides on the event's ``data``; the diagnostic is
    the projection frontends already know how to surface.
    """
    return make_diagnostic(
        code="framework_warning",
        summary=data.summary,
        detail=data.detail if data.HasField("detail") else None,
        scope=DiagnosticScope.DIAGNOSTIC_SCOPE_RUN,
        severity=DiagnosticSeverity.DIAGNOSTIC_SEVERITY_WARNING,
        source=(
            data.source_label
            if data.HasField("source_label")
            else enums.text(events_pb2.FrameworkSource, data.source)
        ),
    )


def _core_failure_diagnostic(event_type: EventType, text: str, data: Message | None) -> Diagnostic:
    """Build a structured diagnostic for a failed core event that lacks one.

    Core events carry unstructured failure facts (event text, a payload error
    string); the projection lifts them into the diagnostic contract so the
    journal invariant holds and frontends need no per-event fallback. Only
    facts the event states are populated: no code beyond the synthesis origin,
    and no hint.
    """
    scope, severity, fallback = _CORE_FAILURE_CONTEXTS[event_type]
    error_text: str | None = None
    if isinstance(data, (events_pb2.AgentExecutionFinishedData, events_pb2.InvocationFinishedData)):
        error_text = data.error if data.HasField("error") else None
    elif isinstance(data, events_pb2.PhaseData):
        fallback = f"Phase {data.phase} failed"
    summary = text or error_text or fallback
    return make_diagnostic(
        code="core_failure",
        summary=summary,
        detail=error_text if error_text is not None and error_text != summary else None,
        scope=scope,
        severity=severity,
        source=enums.text(events_pb2.FrameworkSource, events_pb2.FRAMEWORK_SOURCE_LOOP),
    )


def _core_event_diagnostic(
    event_type: EventType,
    text: str,
    status: EventStatus | None,
    data: Message | None,
) -> Diagnostic | None:
    """Return the diagnostic a projected core event must carry, if any."""
    if isinstance(data, events_pb2.FrameworkWarningData):
        return _framework_warning_diagnostic(data)
    if event_type in _CORE_FAILURE_CONTEXTS and status == EventStatus.EVENT_STATUS_FAILED:
        return _core_failure_diagnostic(event_type, text, data)
    return None


def _wire_payload(data: BaseModel) -> Message:
    """Convert a core event payload to its wire message.

    The core payloads are presentation-neutral and dump to the version 1
    JSON shape, which the upgrade maps onto the typed ``data`` oneof.
    """
    ((field, body),) = upgrade.upgrade_payload(data.model_dump(mode="json")).items()
    return codec.from_dict(_PAYLOAD_TYPES[field], body)


class ServerInvocationLifecycle:
    """Apply operator controls and execution tracking to core calls."""

    def __init__(self, controller: RunController, executions: ExecutionTracker) -> None:
        """Initialize the lifecycle adapter over server control components."""
        self._controller = controller
        self._executions = executions

    def start(  # noqa: PLR0913
        self,
        kind: str,
        round_label: str,
        user_prompt: str,
        system_prompt: str = "",
        *,
        driver: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        participates_in_run_control: bool = True,
    ) -> ExecutionHandle:
        """Apply run control and allocate an execution identity."""
        handle = self._controller.start_agent_execution(
            kind,
            round_label,
            user_prompt,
            system_prompt,
            driver=driver,
            provider=provider,
            model=model,
            participates_in_run_control=participates_in_run_control,
            emit_lifecycle=False,
        )
        return ExecutionHandle(
            execution_id=handle.execution_id,
            user_prompt=handle.user_prompt,
        )

    def finish(
        self,
        kind: str,
        round_label: str,
        *,
        result: Any = None,  # noqa: ANN401
        error: BaseException | None = None,
        execution_id: str | None = None,
    ) -> None:
        """Apply post-invocation control without duplicating core events."""
        self._controller.after_agent(
            kind,
            round_label,
            result=result,
            error=error,
            execution_id=execution_id,
        )

    @contextmanager
    def presentation_scope(
        self,
        *,
        agent_kind: str,
        round_label: str,
        execution_id: str | None,
    ) -> Generator[None]:
        """Scope core presentation events to the active execution."""
        if execution_id is None:
            yield
            return
        with self._executions.presentation_scope(
            agent_kind=agent_kind,
            round_label=round_label,
            invocation_id=execution_id,
        ):
            yield


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
        self.events = CoreEventJournal()
        self.invocations: InvocationLifecycle = ServerInvocationLifecycle(controller, executions)
        self._unsubscribe_core_events = self.events.subscribe(self._project_core_event)
        self._unsubscribe_output = output_sink().subscribe(self._route_output_event)
        self._chat_factory: ExperimentChatFactory | None = None
        self._committed_state_listeners: tuple[CommittedStateListener, ...] = ()
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
        """Attach the core and wire journals to durable run storage."""
        self._failure_diagnostics.clear()
        resolved_run_id = run_id or log_dir.parent.name
        self.events.attach(log_dir, resolved_run_id)
        self.controller.attach(log_dir, project=project, run_id=run_id)

    def add_committed_state_listener(self, listener: CommittedStateListener) -> None:
        """Register an application projection of freshly committed core state."""
        self._committed_state_listeners = (*self._committed_state_listeners, listener)

    def publish_committed_state(
        self,
        namespace: str,
        state: BaseModel,
        *,
        changed_keys: tuple[str, ...] | None = None,
    ) -> None:
        """Synchronously project state while the committed object is stable."""
        project_run = self.project_run
        if project_run is None:
            return
        for listener in self._committed_state_listeners:
            listener(
                namespace,
                project_run.project.root,
                project_run.run_id,
                state,
                changed_keys,
            )

    def attach_run(self, attachment: RunAttachment) -> Callable[[], None] | None:
        """Attach server-only run features and return their cleanup callback."""
        self.attach(
            attachment.log_dir,
            project=attachment.project,
            run_id=attachment.run_id,
        )
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
            supported = supported_cli_providers(resolved_driver)
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
            log_dir=attachment.log_dir,
            defaults=defaults,
            resolve_selection=resolve,
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
        factory, self._chat_factory = self._chat_factory, None
        if factory is not None:
            factory.close()
        self.chat.close_terminal_resource()
        self._unsubscribe_output()
        self._unsubscribe_core_events()

    def record(
        self,
        event_type: EventType,
        text: str = "",
        *,
        data: Message | None = None,
        **fields: Any,  # noqa: ANN401
    ) -> events_pb2.RunEvent:
        """Record a server-only wire event."""
        return self.journal.record(event_type, text, data=data, **fields)

    def read_events(
        self, after_sequence: int = 0, before_sequence: int | None = None
    ) -> list[events_pb2.RunEvent]:
        """Read canonical wire events within an optional cursor range."""
        return self.journal.read(after_sequence, before_sequence)

    def read_history_events(self) -> list[events_pb2.RunEvent]:
        """Read canonical wire history for inspector queries."""
        return self.journal.read_history()

    def _event_diagnostic(
        self,
        event_type: EventType,
        text: str,
        status: EventStatus | None,
        data: Message | None,
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
        if status != EventStatus.EVENT_STATUS_FAILED or event_type not in _EXECUTION_FAILURE_EVENTS:
            return diagnostic
        cached = self._failure_diagnostics.get(execution_id)
        if cached is not None:
            return cached
        self._failure_diagnostics[execution_id] = diagnostic
        return diagnostic

    def _project_core_event(self, event: CoreEvent) -> None:
        event_type = enums.number(events_pb2.EventType, event.type.value)
        data = None if event.data is None else _wire_payload(event.data)
        if event_type in _PRESENTATION_EVENTS and data is not None:
            self.executions.publish_presentation(
                event_type,
                data,
                agent_kind=event.agent_kind,
                round_label=event.round_label,
                invocation_id=event.execution_id,
            )
            return
        terminal_trigger = _TERMINAL_TRIGGERS.get(event_type)
        if terminal_trigger is not None:
            self.controller.settle(terminal_trigger)
        status = (
            enums.number(events_pb2.EventStatus, event.status.value)
            if event.status is not None
            else None
        )
        wire = messages.make_event(
            event_type,
            event.text,
            data=data,
            diagnostic=self._event_diagnostic(
                event_type, event.text, status, data, event.execution_id
            ),
            status=status,
            round_label=event.round_label,
            agent_kind=event.agent_kind,
            execution_id=event.execution_id,
        )
        wire.timestamp.CopyFrom(messages.from_datetime(event.timestamp))
        self.journal.append(wire)

    def _route_output_event(self, event: CoreEvent) -> None:
        if event.agent_kind == "chat":
            self._project_core_event(event)
            return
        self.events.record(event)
