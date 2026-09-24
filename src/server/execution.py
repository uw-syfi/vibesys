"""Active agent execution tracking and presentation-event reduction."""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from server.diagnostics import DiagnosticScope
from server.events import (
    AgentExecutionActivityData,
    AgentExecutionFinishedData,
    AgentExecutionStartedData,
    AgentOutputChannel,
    AgentOutputChunkData,
    EventData,
    EventStatus,
    EventType,
    InvocationFinishedData,
    InvocationStartedData,
    PhaseData,
    TodoUpdateData,
    ToolCallData,
    ToolResultData,
    json_value,
)
from vibesys.api import AgentExecutionStartedData as CoreAgentExecutionStartedData
from vibesys.api import CoreEvent

if TYPE_CHECKING:
    from collections.abc import Generator

    from server.journal import EventJournal
    from vs_agent.api import AgentSelection


@dataclass(frozen=True)
class ExecutionHandle:
    """Identity and effective prompt returned by an execution start boundary."""

    execution_id: str
    user_prompt: str


@dataclass(frozen=True)
class AgentExecutionRequest:
    """One lifecycle request shared by the controller and execution tracker."""

    kind: str
    round_label: str
    user_prompt: str
    system_prompt: str = ""
    participates_in_run_control: bool = True
    emit_lifecycle: bool = True
    agent_selection: AgentSelection | None = None


class ActiveAgentExecution(BaseModel):
    """Authoritative activity checkpoint for one running agent execution."""

    model_config = ConfigDict(extra="forbid")

    execution_id: str
    agent_kind: str
    round_label: str
    stage: str
    attempt: int | None = None
    assignment: str
    started_at: datetime
    activity: AgentExecutionActivityData
    driver: str | None = None
    provider: str | None = None
    model: str | None = None


class ExecutionTracker:
    """Own live execution identity, activity, and lifecycle event emission."""

    def __init__(self, condition: threading.Condition, journal: EventJournal) -> None:
        """Initialize live execution state over the shared server condition."""
        self._condition = condition
        self._journal = journal
        self._active: dict[str, ActiveAgentExecution] = {}
        self._todo_summaries: dict[str, str] = {}
        self._active_tools: dict[str, list[str]] = {}
        self._controlled_ids: set[str] = set()
        self._emitted_lifecycle_ids: set[str] = set()
        self._current_kind: str | None = None
        self._current_round: str | None = None
        self._presentation_local = threading.local()
        self._legacy_local = threading.local()

    @property
    def current_round(self) -> str | None:
        """Return the current controlled execution round."""
        with self._condition:
            return self._current_round

    def current_locked(self) -> tuple[str | None, str | None]:
        """Return current execution labels while the shared lock is held."""
        return self._current_kind, self._current_round

    def active_locked(self) -> list[ActiveAgentExecution]:
        """Copy active execution snapshots while the shared lock is held."""
        return [execution.model_copy(deep=True) for execution in self._active.values()]

    def publish_agent_output(
        self,
        content: str,
        *,
        channel: AgentOutputChannel = "assistant",
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Publish an assistant output chunk when content is nonempty."""
        if content:
            self.publish_presentation(
                EventType.AGENT_OUTPUT_CHUNK,
                AgentOutputChunkData(channel=channel, content=content),
                agent_kind=agent_kind,
                round_label=round_label,
                invocation_id=invocation_id,
            )

    def publish_presentation(
        self,
        event_type: EventType,
        data: EventData,
        *,
        agent_kind: str | None = None,
        round_label: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        """Record one presentation event and reduce its activity state."""
        scoped_kind = getattr(self._presentation_local, "agent_kind", None)
        scoped_round = getattr(self._presentation_local, "round_label", None)
        scoped_invocation = getattr(self._presentation_local, "invocation_id", None)
        scoped_chat_thread = getattr(self._presentation_local, "chat_thread_id", None)
        execution_id = invocation_id or scoped_invocation
        if execution_id is not None:
            activity = self._activity_for_presentation(event_type, data, execution_id)
            if activity is not None:
                self.update_activity(execution_id, activity)
        with self._condition:
            current_kind, current_round = self.current_locked()
        self._journal.record(
            event_type,
            agent_kind=agent_kind or scoped_kind or current_kind,
            round_label=round_label or scoped_round or current_round,
            execution_id=execution_id,
            chat_thread_id=scoped_chat_thread,
            data=data,
        )

    def update_activity(self, execution_id: str, activity: AgentExecutionActivityData) -> None:
        """Update one active execution when its activity has changed."""
        with self._condition:
            active = self._active.get(execution_id)
            if active is None or active.activity == activity:
                return
            self._journal.record(
                EventType.AGENT_EXECUTION_ACTIVITY_CHANGED,
                status=EventStatus.ACTIVE,
                agent_kind=active.agent_kind,
                round_label=active.round_label,
                execution_id=execution_id,
                data=activity,
            )
            self._active[execution_id] = active.model_copy(update={"activity": activity})

    def start_locked(
        self,
        request: AgentExecutionRequest,
        *,
        execution_id: str | None = None,
    ) -> ExecutionHandle:
        """Allocate and track an execution while the shared lock is held.

        ``execution_id`` lets a caller that already minted an identity (core,
        through the ``AGENT_EXECUTION_STARTED`` projection) supply it; direct
        callers that have no identity of their own keep getting one minted
        here.
        """
        execution_id = execution_id or uuid.uuid4().hex
        attempt = _attempt_from_label(request.round_label)
        driver = request.agent_selection.driver if request.agent_selection is not None else None
        provider = request.agent_selection.provider if request.agent_selection is not None else None
        model = request.agent_selection.model if request.agent_selection is not None else None
        activity = AgentExecutionActivityData(
            mode="thinking", summary=_initial_activity_summary(request.kind)
        )
        active = ActiveAgentExecution(
            execution_id=execution_id,
            agent_kind=request.kind,
            round_label=request.round_label,
            stage=request.kind,
            attempt=attempt,
            assignment=request.user_prompt,
            started_at=datetime.now(UTC),
            activity=activity,
            driver=driver,
            provider=provider,
            model=model,
        )
        if request.emit_lifecycle:
            self._journal.record(
                EventType.AGENT_EXECUTION_STARTED,
                status=EventStatus.ACTIVE,
                agent_kind=request.kind,
                round_label=request.round_label,
                execution_id=execution_id,
                data=AgentExecutionStartedData(
                    stage=request.kind,
                    attempt=attempt,
                    system_prompt=request.system_prompt,
                    user_prompt=request.user_prompt,
                    activity=activity,
                    driver=driver,
                    provider=provider,
                    model=model,
                ),
            )
            self._journal.record(
                EventType.PHASE_STARTED,
                status=EventStatus.ACTIVE,
                agent_kind=request.kind,
                round_label=request.round_label,
                execution_id=execution_id,
                data=PhaseData(phase=request.kind, attempt=attempt),
            )
            self._journal.record(
                EventType.INVOCATION_STARTED,
                status=EventStatus.ACTIVE,
                agent_kind=request.kind,
                round_label=request.round_label,
                execution_id=execution_id,
                data=InvocationStartedData(
                    system_prompt=request.system_prompt,
                    user_prompt=request.user_prompt,
                ),
            )
        if request.participates_in_run_control:
            self._current_kind, self._current_round = request.kind, request.round_label
        self._active[execution_id] = active
        if request.participates_in_run_control:
            self._controlled_ids.add(execution_id)
        if request.emit_lifecycle:
            self._emitted_lifecycle_ids.add(execution_id)
        return ExecutionHandle(execution_id=execution_id, user_prompt=request.user_prompt)

    def finish_locked(
        self,
        execution_id: str,
        *,
        result: object | None = None,
        error: BaseException | None = None,
    ) -> tuple[ActiveAgentExecution | None, bool]:
        """Finish a tracked execution while the shared lock is held."""
        active = self._active.get(execution_id)
        if active is None:
            return None, False
        controlled = execution_id in self._controlled_ids
        emits_lifecycle = execution_id in self._emitted_lifecycle_ids
        if not emits_lifecycle:
            self._discard_locked(execution_id)
            return active, controlled
        terminal_status = (
            _execution_error_status(error) if error is not None else EventStatus.COMPLETED
        )
        if error is not None:
            execution_event = self._journal.record_failure(
                EventType.AGENT_EXECUTION_FINISHED,
                error,
                scope=DiagnosticScope.INVOCATION,
                operation="Agent execution",
                status=terminal_status,
                data_factory=lambda diagnostic: AgentExecutionFinishedData(
                    result=json_value(result), error=diagnostic.summary
                ),
                agent_kind=active.agent_kind,
                round_label=active.round_label,
                execution_id=execution_id,
            )
            diagnostic = execution_event.diagnostic
        else:
            self._journal.record(
                EventType.AGENT_EXECUTION_FINISHED,
                status=EventStatus.COMPLETED,
                data=AgentExecutionFinishedData(result=json_value(result)),
                agent_kind=active.agent_kind,
                round_label=active.round_label,
                execution_id=execution_id,
            )
            diagnostic = None
        legacy_finished = InvocationFinishedData(
            result=json_value(result),
            error=diagnostic.summary if diagnostic else None,
        )
        if error is not None:
            for event_type, data in (
                (EventType.INVOCATION_FINISHED, legacy_finished),
                (
                    EventType.PHASE_FINISHED,
                    PhaseData(phase=active.stage, attempt=active.attempt),
                ),
            ):
                self._journal.record_failure(
                    event_type,
                    error,
                    scope=DiagnosticScope.INVOCATION,
                    operation="Agent execution",
                    status=terminal_status,
                    data=data,
                    diagnostic=diagnostic,
                    agent_kind=active.agent_kind,
                    round_label=active.round_label,
                    execution_id=execution_id,
                )
        else:
            self._journal.record(
                EventType.INVOCATION_FINISHED,
                status=EventStatus.COMPLETED,
                agent_kind=active.agent_kind,
                round_label=active.round_label,
                execution_id=execution_id,
                data=legacy_finished,
            )
            self._journal.record(
                EventType.PHASE_FINISHED,
                status=EventStatus.COMPLETED,
                agent_kind=active.agent_kind,
                round_label=active.round_label,
                execution_id=execution_id,
                data=PhaseData(phase=active.stage, attempt=active.attempt),
            )
        self._discard_locked(execution_id)
        return active, controlled

    def track_started(self, event: CoreEvent) -> None:
        """Track a core-minted execution start; no-op if already tracked.

        Core mints the execution id and emits ``AGENT_EXECUTION_STARTED``
        itself (see ``vibesys.context._RunContext.invoke``); this projects
        that event into the same ``ActiveAgentExecution`` checkpoint
        ``start_locked`` builds. The guard against an already-active id
        covers a caller (``RunController.start_agent_execution``) that has
        already allocated the execution before core's event arrives, so the
        two allocation paths never double-track the same identity.
        """
        if (
            event.execution_id is None
            or event.agent_kind is None
            or event.round_label is None
            or not isinstance(event.data, CoreAgentExecutionStartedData)
        ):
            return
        data = event.data
        activity = AgentExecutionActivityData(
            mode=data.activity.mode, summary=data.activity.summary, tool=data.activity.tool
        )
        active = ActiveAgentExecution(
            execution_id=event.execution_id,
            agent_kind=event.agent_kind,
            round_label=event.round_label,
            stage=data.stage,
            attempt=data.attempt,
            assignment=data.user_prompt,
            started_at=event.timestamp,
            activity=activity,
            driver=data.driver,
            provider=data.provider,
            model=data.model,
        )
        with self._condition:
            if event.execution_id in self._active:
                return
            self._active[event.execution_id] = active
            self._controlled_ids.add(event.execution_id)
            self._current_kind, self._current_round = event.agent_kind, event.round_label

    def discard_finished(self, event: CoreEvent) -> None:
        """Drop a core-projected execution's tracking state once it finishes."""
        if event.execution_id is None:
            return
        with self._condition:
            self._discard_locked(event.execution_id)

    def interrupt_controlled_locked(self) -> None:
        """Interrupt all controlled executions during run termination."""
        for execution_id, active in tuple(self._active.items()):
            if execution_id not in self._controlled_ids:
                continue
            message = "Run ended before the agent execution completed"
            self._journal.record(
                EventType.AGENT_EXECUTION_FINISHED,
                message,
                status=EventStatus.INTERRUPTED,
                data=AgentExecutionFinishedData(error=message),
                agent_kind=active.agent_kind,
                round_label=active.round_label,
                execution_id=execution_id,
            )
            self._discard_locked(execution_id)
            self._journal.record(
                EventType.INVOCATION_FINISHED,
                message,
                status=EventStatus.INTERRUPTED,
                data=InvocationFinishedData(error=message),
                agent_kind=active.agent_kind,
                round_label=active.round_label,
                execution_id=execution_id,
            )
            self._journal.record(
                EventType.PHASE_FINISHED,
                message,
                status=EventStatus.INTERRUPTED,
                data=PhaseData(phase=active.stage, attempt=active.attempt),
                agent_kind=active.agent_kind,
                round_label=active.round_label,
                execution_id=execution_id,
            )

    def remember_legacy(self, execution_id: str) -> None:
        """Remember an execution for the prompt-only compatibility boundary."""
        self._legacy_local.execution_id = execution_id

    def resolve_legacy(self, execution_id: str | None) -> str | None:
        """Resolve an explicit or thread-local compatibility execution id."""
        return execution_id or getattr(self._legacy_local, "execution_id", None)

    def clear_legacy(self, execution_id: str) -> None:
        """Clear the matching thread-local compatibility execution id."""
        if getattr(self._legacy_local, "execution_id", None) == execution_id:
            self._legacy_local.execution_id = None

    @contextmanager
    def presentation_scope(
        self,
        *,
        agent_kind: str,
        round_label: str,
        invocation_id: str,
        chat_thread_id: str | None = None,
    ) -> Generator[None]:
        """Scope presentation events to one execution on the current thread.

        ``chat_thread_id`` names the experiment-chat thread whose question this
        execution answers, so streamed output reaches the transcript that asked
        for it. ``None`` covers both a non-chat agent and the run's default
        chat, which is how the terminal ``chat`` answer identifies that thread.
        """
        previous = (
            getattr(self._presentation_local, "agent_kind", None),
            getattr(self._presentation_local, "round_label", None),
            getattr(self._presentation_local, "invocation_id", None),
            getattr(self._presentation_local, "chat_thread_id", None),
        )
        self._presentation_local.agent_kind = agent_kind
        self._presentation_local.round_label = round_label
        self._presentation_local.invocation_id = invocation_id
        self._presentation_local.chat_thread_id = chat_thread_id
        try:
            yield
        finally:
            (
                self._presentation_local.agent_kind,
                self._presentation_local.round_label,
                self._presentation_local.invocation_id,
                self._presentation_local.chat_thread_id,
            ) = previous

    def _discard_locked(self, execution_id: str) -> None:
        self._active.pop(execution_id, None)
        self._controlled_ids.discard(execution_id)
        self._emitted_lifecycle_ids.discard(execution_id)
        self._todo_summaries.pop(execution_id, None)
        self._active_tools.pop(execution_id, None)

    def _activity_for_presentation(
        self, event_type: EventType, data: EventData, execution_id: str
    ) -> AgentExecutionActivityData | None:
        if event_type is EventType.AGENT_OUTPUT_CHUNK and isinstance(data, AgentOutputChunkData):
            return self._output_activity(data, execution_id)
        if event_type is EventType.TOOL_CALL and isinstance(data, ToolCallData):
            return self._tool_call_activity(data, execution_id)
        if event_type is EventType.TODO_UPDATE and isinstance(data, TodoUpdateData):
            return self._todo_activity(data, execution_id)
        if event_type is EventType.TOOL_RESULT and isinstance(data, ToolResultData):
            return self._tool_result_activity(data, execution_id)
        return None

    def _output_activity(
        self, data: AgentOutputChunkData, execution_id: str
    ) -> AgentExecutionActivityData | None:
        with self._condition:
            if self._active_tools.get(execution_id):
                return None
        return _text_activity(data)

    def _tool_call_activity(
        self, data: ToolCallData, execution_id: str
    ) -> AgentExecutionActivityData:
        with self._condition:
            self._active_tools.setdefault(execution_id, []).append(data.tool)
        return AgentExecutionActivityData(mode="tool", summary=f"Using {data.tool}", tool=data.tool)

    def _todo_activity(
        self, data: TodoUpdateData, execution_id: str
    ) -> AgentExecutionActivityData | None:
        current = next((todo.content for todo in data.todos if todo.status == "in_progress"), None)
        with self._condition:
            if current is None:
                self._todo_summaries.pop(execution_id, None)
                if self._active_tools.get(execution_id):
                    return None
                return AgentExecutionActivityData(mode="thinking", summary="Thinking")
            self._todo_summaries[execution_id] = current
            if self._active_tools.get(execution_id):
                return None
        return AgentExecutionActivityData(mode="thinking", summary=current)

    def _tool_result_activity(
        self, data: ToolResultData, execution_id: str
    ) -> AgentExecutionActivityData | None:
        with self._condition:
            if execution_id not in self._active:
                return None
            tools = self._active_tools.get(execution_id, [])
            if data.tool in tools:
                tools.remove(data.tool)
            remaining_tool = tools[-1] if tools else None
            todo_summary = self._todo_summaries.get(execution_id)
        if remaining_tool is not None:
            return AgentExecutionActivityData(
                mode="tool", summary=f"Using {remaining_tool}", tool=remaining_tool
            )
        return AgentExecutionActivityData(mode="thinking", summary=todo_summary or "Thinking")


def _attempt_from_label(round_label: str) -> int | None:
    match = re.search(r"retry-(\d+)", round_label)
    return int(match.group(1)) if match else None


def _execution_error_status(error: BaseException) -> EventStatus:
    if isinstance(error, asyncio.CancelledError) or type(error).__name__ == "CancelledError":
        return EventStatus.CANCELLED
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return EventStatus.INTERRUPTED
    return EventStatus.FAILED


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


def _text_activity(data: AgentOutputChunkData) -> AgentExecutionActivityData | None:
    if data.channel == "analysis":
        return AgentExecutionActivityData(mode="thinking", summary="Thinking")
    if data.channel == "assistant":
        return AgentExecutionActivityData(mode="responding", summary="Responding")
    return None
