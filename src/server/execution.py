"""Active agent execution tracking and presentation-event reduction."""

from __future__ import annotations

import asyncio
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from collections.abc import Generator

    from server.journal import EventJournal


@dataclass(frozen=True)
class ExecutionHandle:
    """Identity and effective prompt returned by an execution start boundary."""

    execution_id: str
    user_prompt: str


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

    def start_locked(  # noqa: PLR0913
        self,
        kind: str,
        round_label: str,
        effective_prompt: str,
        system_prompt: str,
        *,
        participates_in_run_control: bool,
        emit_lifecycle: bool,
        driver: str | None,
        provider: str | None,
        model: str | None,
    ) -> ExecutionHandle:
        """Allocate and track an execution while the shared lock is held."""
        execution_id = uuid.uuid4().hex
        attempt = _attempt_from_label(round_label)
        activity = AgentExecutionActivityData(
            mode="thinking", summary=_initial_activity_summary(kind)
        )
        active = ActiveAgentExecution(
            execution_id=execution_id,
            agent_kind=kind,
            round_label=round_label,
            stage=kind,
            attempt=attempt,
            assignment=effective_prompt,
            started_at=datetime.now(UTC),
            activity=activity,
            driver=driver,
            provider=provider,
            model=model,
        )
        if emit_lifecycle:
            self._journal.record(
                EventType.AGENT_EXECUTION_STARTED,
                status=EventStatus.ACTIVE,
                agent_kind=kind,
                round_label=round_label,
                execution_id=execution_id,
                data=AgentExecutionStartedData(
                    stage=kind,
                    attempt=attempt,
                    system_prompt=system_prompt,
                    user_prompt=effective_prompt,
                    activity=activity,
                    driver=driver,
                    provider=provider,
                    model=model,
                ),
            )
            self._journal.record(
                EventType.PHASE_STARTED,
                status=EventStatus.ACTIVE,
                agent_kind=kind,
                round_label=round_label,
                execution_id=execution_id,
                data=PhaseData(phase=kind, attempt=attempt),
            )
            self._journal.record(
                EventType.INVOCATION_STARTED,
                status=EventStatus.ACTIVE,
                agent_kind=kind,
                round_label=round_label,
                execution_id=execution_id,
                data=InvocationStartedData(
                    system_prompt=system_prompt,
                    user_prompt=effective_prompt,
                ),
            )
        if participates_in_run_control:
            self._current_kind, self._current_round = kind, round_label
        self._active[execution_id] = active
        if participates_in_run_control:
            self._controlled_ids.add(execution_id)
        if emit_lifecycle:
            self._emitted_lifecycle_ids.add(execution_id)
        return ExecutionHandle(execution_id=execution_id, user_prompt=effective_prompt)

    def finish_locked(
        self,
        execution_id: str,
        *,
        result: Any = None,  # noqa: ANN401
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

    def _activity_for_presentation(  # noqa: C901, PLR0911
        self, event_type: EventType, data: EventData, execution_id: str
    ) -> AgentExecutionActivityData | None:
        if event_type is EventType.AGENT_OUTPUT_CHUNK and isinstance(data, AgentOutputChunkData):
            with self._condition:
                if self._active_tools.get(execution_id):
                    return None
            return _text_activity(data)
        if event_type is EventType.TOOL_CALL and isinstance(data, ToolCallData):
            with self._condition:
                self._active_tools.setdefault(execution_id, []).append(data.tool)
            return AgentExecutionActivityData(
                mode="tool", summary=f"Using {data.tool}", tool=data.tool
            )
        if event_type is EventType.TODO_UPDATE and isinstance(data, TodoUpdateData):
            current = next(
                (todo.content for todo in data.todos if todo.status == "in_progress"),
                None,
            )
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
        if event_type is EventType.TOOL_RESULT and isinstance(data, ToolResultData):
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
        return None


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
