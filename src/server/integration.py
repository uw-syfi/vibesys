"""Adapter from core run ports to frontend-serving components."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from server.chat.factory import (
    ChatAgentBuilder,
    ExperimentChatFactory,
    build_chat_agent,
)
from server.diagnostics import Diagnostic, DiagnosticScope, DiagnosticSeverity
from server.events import (
    AgentExecutionFinishedData,
    EventData,
    EventStatus,
    EventType,
    FrameworkSource,
    FrameworkWarningData,
    InputMeasurementFailedData,
    InvocationFinishedData,
    PhaseData,
    RunEvent,
    SteerProposedData,
    ToolResultData,
    TurnUsageData,
)
from server.read_model import RunInspector
from server.run_attachment import AgentSelection, RunAttachment
from server.run_lifecycle import RunTrigger
from vibesys.api import (
    STEER_PROPOSAL_KIND,
    CoreEventType,
    JsonResultPayload,
    SteerDeliveryData,
    SteerProposalReceipt,
)
from vs_sim.api import CloseOnce, ReleaseStack

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from server.chat.manager import ChatManager
    from server.controller import RunController
    from server.execution import ExecutionTracker
    from server.journal import WireJournal
    from vibesys.api import AuxiliaryAgents, CoreEvent, RunReader, RunReady, RunRecord, RunSession
    from vs_sim.api import Clock, Threads

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
        EventType.RATE_LIMIT_UPDATE,
        EventType.QUOTA_PAUSED,
        EventType.QUOTA_RESUMED,
        EventType.QUOTA_ABANDONED,
        EventType.QUOTA_WAITING,
        EventType.QUOTA_WAIT_CANCELLED,
        EventType.PROVIDER_SWITCHED,
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
        CoreEventType.GUIDANCE_QUEUED,
        CoreEventType.WITHDRAW_QUEUED,
        CoreEventType.GUIDANCE_ENDED,
        CoreEventType.GUIDANCE_REACHED,
        CoreEventType.PAUSE_REQUESTED,
        CoreEventType.RESUMED,
        CoreEventType.STOP_REQUESTED,
        CoreEventType.STEER_CONSUMED,
        CoreEventType.STEER_DELIVERED_MID_TURN,
        CoreEventType.STEER_DROPPED,
        CoreEventType.CONTROL_REFUSED,
        CoreEventType.REQUEST_REJECTED,
        CoreEventType.PAUSED,
        CoreEventType.STOPPED,
        CoreEventType.ACTION_QUEUED,
        CoreEventType.ACTION_APPLIED,
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


def _input_measurement_diagnostic(data: InputMeasurementFailedData) -> Diagnostic:
    """Lift a failed input measurement into the run-scoped diagnostic surface (S173).

    The run goes on, so this is an error, not a fatal one. The summary states the facts the
    event carries: how it failed, where, how often it was measured, and what candidates are
    judged against now; the detail is the redacted tail of what the failed stage printed.
    """
    where = f", stage {data.stage}" if data.stage else ""
    twice = "; measured twice" if data.attempts > 1 else ""
    judged = (
        "candidates must still beat its benchmark"
        if data.compared
        else "a candidate adopted now is adopted without an input comparison"
    )
    return Diagnostic(
        code="input_measurement_failed",
        summary=f"The input's trusted measurement failed ({data.failure.value}{where}{twice}); "
        f"{judged}",
        detail=data.summary or None,
        scope=DiagnosticScope.RUN,
        severity=DiagnosticSeverity.ERROR,
        source=FrameworkSource.LOOP.value,
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
    if isinstance(data, InputMeasurementFailedData):
        return _input_measurement_diagnostic(data)
    if event_type in _CORE_FAILURE_CONTEXTS and status is EventStatus.FAILED:
        return _core_failure_diagnostic(event_type, text, data)
    return None


def _steer_proposal(data: ToolResultData, execution_id: str | None) -> SteerProposedData | None:
    """The proposal a chat turn's tool result carries, or None when it carries none.

    ``propose_steer`` returns a ``SteerProposalReceipt`` as JSON. The receipt is recognized by its
    ``kind`` and parsed strictly, so a result that merely mentions a steer, or an error, proposes
    nothing. The proposal is named by the turn and the tool call when the provider gives the call
    an id (stable across replays), and by a fresh id otherwise.
    """
    payload = data.payload
    if data.is_error or not isinstance(payload, JsonResultPayload):
        return None
    value = payload.value
    if not isinstance(value, dict) or value.get("kind") != STEER_PROPOSAL_KIND:
        return None
    try:
        receipt = SteerProposalReceipt.model_validate(value)
    except ValidationError:
        return None
    stable = execution_id is not None and data.call_id is not None
    return SteerProposedData(
        proposal_id=f"{execution_id}.{data.call_id}" if stable else uuid.uuid4().hex,
        proposal=receipt.proposal,
    )


class RunIntegrationAdapter:
    """Implement the neutral core integration port with server components."""

    # lint-waiver: LW-731410 [PLR0913]; four server services, the run clock and threads, and the chat builder.
    # > Bundling the services would only rename what ServerRuntime and the tests already build
    # > one by one; the clock, threads and builder are independent seams.
    def __init__(  # noqa: PLR0913
        self,
        controller: RunController,
        executions: ExecutionTracker,
        journal: WireJournal,
        chat: ChatManager,
        *,
        clock: Clock,
        threads: Threads,
        chat_agent_builder: ChatAgentBuilder = build_chat_agent,
    ) -> None:
        """Compose the core port over independently testable server services.

        ``clock`` counts seconds since the epoch; experiment chat stamps its threads with it.
        ``threads`` makes experiment chat's locks and workers.
        """
        self.controller = controller
        self.executions = executions
        self.journal = journal
        self.chat = chat
        self._clock = clock
        self._threads = threads
        self._chat_agent_builder = chat_agent_builder
        self._chat_factory: ExperimentChatFactory | None = None
        self._owned = ReleaseStack()
        self._detach_run: Callable[[], None] | None = None
        self._close = CloseOnce()
        self._failure_diagnostics: dict[str, Diagnostic] = {}
        self.trace = journal.trace
        """The run's spans, folded from its span events (live and replayed)."""

    @property
    def attached_run(self) -> RunRecord | None:
        """Return the attached semantic run record, if available."""
        return self.controller.attached_run

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
        run_id: str,
        record: RunRecord | None = None,
    ) -> None:
        """Attach the wire journal of ``run_id`` to durable run storage."""
        self._failure_diagnostics.clear()
        self.controller.attach(log_dir, run_id=run_id, record=record)

    def handle_run_ready(self, session: RunSession, ready: RunReady) -> None:
        """Attach frontend projection and chat from narrow readiness facts.

        Registered as this run's sole ``RunSession.on_ready`` listener. Durable
        attach runs first so the wire journal is ready before experiment chat
        can read the run history.
        """
        self.attach(ready.log_directory, run_id=ready.record.run_id, record=ready.record)
        self.controller.ready()
        self._open_chat(ready, session.open_auxiliary_agents())

    def attach_reader(self, reader: RunReader) -> None:
        """Attach experiment chat over a finished run's reader: the run's journal is already open.

        Only chat is attached. The run is not announced ready and nothing of it executes.
        """
        self._owned.callback(reader.close)
        self._open_chat(reader.ready(), reader.open_auxiliary_agents())

    def _open_chat(self, ready: RunReady, auxiliary_agents: AuxiliaryAgents) -> None:
        """Own ``auxiliary_agents`` and start experiment chat over them."""
        attachment = RunAttachment(
            chat_state_dir=ready.frontend_state_directory / "chat",
            agent_defaults=AgentSelection(
                provider=ready.agent_provider,
                model=ready.agent_model,
                role_models=ready.role_models,
            ),
            agent_providers=ready.agent_providers,
        )
        self._owned.callback(auxiliary_agents.close)
        self._attach_run(attachment, auxiliary_agents)

    def _attach_run(self, attachment: RunAttachment, session: AuxiliaryAgents) -> None:
        """Own the optional experiment-chat surface before starting it."""
        previous = self._chat_factory
        if previous is not None:
            previous.close()
        factory = ExperimentChatFactory(
            manager=self.chat,
            controller=self.controller,
            executions=self.executions,
            session=session,
            attachment=attachment,
            build_agent=self._chat_agent_builder,
            fallback=RunInspector(self).answer,
            clock=self._clock,
            threads=self._threads,
        )

        def detach() -> None:
            if self._chat_factory is factory:
                self._chat_factory = None
            factory.close()

        self._detach_run = detach
        self._chat_factory = factory
        try:
            factory.start()
        except Exception as exc:  # noqa: BLE001  # lint-waiver: LW-009031 [BLE001]; optional experiment chat startup failures are reported without failing the core run.
            self.journal.publish_output(
                "stderr",
                f"Experiment chat is unavailable: {type(exc).__name__}: {exc}\n",
                source="experiment-chat",
            )

    def close(self) -> None:
        """Release adapter subscriptions and optional server resources.

        A step whose close raised runs again on the next call.
        """
        if not self._close.started:
            self._owned.callback(self.chat.close_terminal_resource)
            detach_run, self._detach_run = self._detach_run, None
            if detach_run is not None:
                self._owned.callback(detach_run)
        self._close.run(self._owned.close)

    def record(
        self,
        event_type: EventType,
        text: str = "",
        *,
        data: EventData | None = None,
        **fields: object,
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
        if isinstance(data, TurnUsageData) and data.span_id is None:
            data = data.model_copy(update={"span_id": self.trace.turn_span(event.execution_id)})
        if event_type is EventType.AGENT_EXECUTION_STARTED:
            self.executions.track_started(event)
        elif event_type is EventType.AGENT_EXECUTION_FINISHED:
            self.executions.discard_finished(event)
            self.controller.reach_invocation_boundary(
                event.agent_kind, event.round_label, event.execution_id
            )
        if event_type in _PRESENTATION_EVENTS:
            self._route_output_event(event)
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
        handle = {
            CoreEventType.STEER_QUEUED: lambda: self.controller.steer(event.text),
            CoreEventType.GUIDANCE_QUEUED: lambda: self.controller.standing_guidance(event.text),
            CoreEventType.WITHDRAW_QUEUED: lambda: self.controller.withdraw(event.text),
            CoreEventType.GUIDANCE_ENDED: lambda: self.controller.record_guidance_ended(event.text),
            CoreEventType.PAUSE_REQUESTED: self.controller.pause_after_call,
            CoreEventType.RESUMED: self.controller.resume,
            CoreEventType.STOP_REQUESTED: self.controller.stop_after_call,
            CoreEventType.STEER_CONSUMED: lambda: self.controller.record_steer_consumed(
                agent_kind=event.agent_kind,
                round_label=event.round_label,
                execution_id=event.execution_id,
                note=event.text,
            ),
            CoreEventType.STEER_DELIVERED_MID_TURN: lambda: self.controller.record_steer_consumed(
                agent_kind=event.agent_kind,
                round_label=event.round_label,
                execution_id=event.execution_id,
                mid_turn=True,
            ),
            CoreEventType.STEER_DROPPED: lambda: self.controller.record_steer_dropped(event.text),
            CoreEventType.CONTROL_REFUSED: lambda: self.controller.record_control_refused(
                event.text
            ),
            CoreEventType.REQUEST_REJECTED: lambda: self.controller.record_request_rejected(
                event.text
            ),
            CoreEventType.ACTION_QUEUED: lambda: self.controller.action(event.text),
            CoreEventType.ACTION_APPLIED: lambda: self.controller.record_action_applied(event.text),
            CoreEventType.PAUSED: self.controller.land_pause_at_boundary,
            CoreEventType.STOPPED: self.controller.land_stop_at_boundary,
        }.get(event.type)
        if handle is not None:
            handle()
        if isinstance(event.data, SteerDeliveryData):
            self.journal.record(
                EventType.STEER_DELIVERY,
                data=event.data,
                agent_kind=event.agent_kind,
                round_label=event.round_label,
                execution_id=event.execution_id,
            )

    def _route_output_event(self, event: CoreEvent) -> None:
        """Publish one explicitly delivered presentation event."""
        if self._close.started:
            return
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
        if event.agent_kind == "chat" and isinstance(data, ToolResultData):
            proposed = _steer_proposal(data, event.execution_id)
            if proposed is not None:
                self.executions.publish_presentation(
                    EventType.STEER_PROPOSED,
                    proposed,
                    agent_kind=event.agent_kind,
                    round_label=event.round_label,
                    invocation_id=event.execution_id,
                )
