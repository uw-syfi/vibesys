"""Adapter from core run ports to frontend-serving components."""

from __future__ import annotations

from contextlib import contextmanager
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
    EventData,
    EventStatus,
    EventType,
    FrameworkWarningData,
    RunEvent,
)
from server.run_lifecycle import RunTrigger
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
    from collections.abc import Callable, Generator
    from pathlib import Path

    from server.chat.manager import ChatManager
    from server.controller import ProjectRunState, RunController
    from server.execution import ExecutionTracker
    from server.journal import EventJournal as WireEventJournal
    from vibesys.run.events import CoreEvent
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
        self._closed = False

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
        resolved_run_id = run_id or log_dir.parent.name
        self.events.attach(log_dir, resolved_run_id)
        self.controller.attach(log_dir, project=project, run_id=run_id)

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

        from server.read_model import RunInspector  # noqa: PLC0415

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

    def _project_core_event(self, event: CoreEvent) -> None:
        event_type = EventType(event.type.value)
        data = (
            None
            if event.data is None
            else _EVENT_DATA_ADAPTER.validate_python(event.data.model_dump(mode="python"))
        )
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
        self.journal.append(
            RunEvent(
                timestamp=event.timestamp,
                type=event_type,
                text=event.text,
                diagnostic=(
                    _framework_warning_diagnostic(data)
                    if isinstance(data, FrameworkWarningData)
                    else None
                ),
                status=(EventStatus(event.status.value) if event.status is not None else None),
                round_label=event.round_label,
                agent_kind=event.agent_kind,
                execution_id=event.execution_id,
                data=data,
            )
        )

    def _route_output_event(self, event: CoreEvent) -> None:
        if event.agent_kind == "chat":
            self._project_core_event(event)
            return
        self.events.record(event)
