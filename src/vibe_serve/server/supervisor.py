"""Thread-safe human controls at agent invocation boundaries."""

from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path
from typing import Any

from vibe_serve.server.events import (
    AgentOutputChunkData,
    ChatData,
    EventData,
    EventStatus,
    EventStore,
    EventType,
    InvocationFinishedData,
    InvocationStartedData,
    OutputData,
    PhaseData,
    RunEvent,
    json_value,
    make_event,
)
from vibe_serve.server.protocol import RunSnapshot


class RunSupervisor:
    """Own pause state, invocation metadata, and the run audit store."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._pause_after_call = False
        self._paused = False
        self._active_invocation: str | None = None
        self._run_status = "starting"
        self._store: EventStore | None = None
        self._audit_store: EventStore | None = None
        self._pending_events: list[RunEvent] = []
        self.log_dir: Path | None = None
        self._current_kind: str | None = None
        self._current_round: str | None = None

    @property
    def current_round(self) -> str | None:
        with self._condition:
            return self._current_round

    def attach(self, log_dir: Path) -> None:
        log_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir = log_dir
        events_path = log_dir / "run-events.jsonl"
        with self._condition:
            store = self._store
            if store is not None and (store.path == events_path or self._audit_store is not None):
                return
            if store is None:
                store = EventStore(events_path, run_id=log_dir.parent.name)
                self._store = store
                pending, self._pending_events = self._pending_events, []
            else:
                self._audit_store = EventStore(events_path, run_id=log_dir.parent.name)
                pending = store.read()
        for event in pending:
            (self._audit_store or store).append(event)
        if self._audit_store is None:
            self.record(EventType.SERVER_STARTED, status=EventStatus.ACTIVE)
        with self._condition:
            self._run_status = "running"

    def publish_output(self, stream: str, content: str, source: str = "backend") -> None:
        if not content:
            return
        self.record(
            EventType.OUTPUT,
            data=OutputData(stream=stream, source=source, content=content),
        )

    def publish_agent_output(
        self,
        content: str,
        *,
        channel: str = "assistant",
        agent_kind: str | None = None,
        invocation_id: str | None = None,
    ) -> None:
        if not content:
            return
        self.record(
            EventType.AGENT_OUTPUT_CHUNK,
            agent_kind=agent_kind or self._current_kind,
            round_label=self._current_round,
            invocation_id=invocation_id or self._active_invocation,
            data=AgentOutputChunkData(channel=channel, content=content),
        )

    def record(
        self,
        event_type: EventType,
        text: str = "",
        *,
        data: EventData | None = None,
        **fields: Any,
    ) -> RunEvent | None:
        event = make_event(event_type, text, data=data, **fields)
        with self._condition:
            store = self._store
            if store is None:
                self._pending_events.append(event)
                return event
        recorded = store.append(event)
        audit_store = self._audit_store
        if audit_store is not None:
            audit_store.append(event)
        return recorded

    def read_events(self, after_sequence: int = 0) -> list[RunEvent]:
        store = self._store
        return store.read(after_sequence) if store else []

    def read_history_events(self) -> list[RunEvent]:
        """Return the durable session history, including earlier attachments."""
        store = self._audit_store or self._store
        return store.read() if store else []

    def wait_for_events(self, after_sequence: int, timeout: float | None = None) -> list[RunEvent]:
        store = self._store
        return store.wait(after_sequence, timeout) if store else []

    def snapshot(self) -> RunSnapshot:
        with self._condition:
            store = self._store
            return RunSnapshot(
                run_id=store.run_id if store else "",
                sequence=store.last_sequence if store else 0,
                status="paused" if self._paused else self._run_status,
                agent_kind=self._current_kind,
                round_label=self._current_round,
            )

    def chat(self, text: str) -> str:
        from vibe_serve.server.inspector import RunInspector

        answer = RunInspector(self).answer(text)
        self.record(
            EventType.CHAT,
            text,
            status=EventStatus.ANSWERED,
            data=ChatData(answer=answer),
        )
        return answer

    def pause_after_call(self) -> None:
        with self._condition:
            self._pause_after_call = True
        self.record(EventType.CONTROL, "/pause", status=EventStatus.PENDING)

    def resume(self) -> None:
        with self._condition:
            self._paused = False
            self._pause_after_call = False
            self._condition.notify_all()
        self.record(EventType.CONTROL, "/resume", status=EventStatus.CONSUMED)

    def before_agent(
        self, kind: str, round_label: str, user_prompt: str, system_prompt: str = ""
    ) -> None:
        with self._condition:
            while self._paused:
                self._condition.wait()
            self._current_kind, self._current_round = kind, round_label
            invocation_id = uuid.uuid4().hex
            self._active_invocation = invocation_id

        phase = PhaseData(phase=kind, attempt=_attempt_from_label(round_label))
        self.record(
            EventType.PHASE_STARTED,
            status=EventStatus.ACTIVE,
            agent_kind=kind,
            round_label=round_label,
            invocation_id=invocation_id,
            data=phase,
        )
        self.record(
            EventType.INVOCATION_STARTED,
            status=EventStatus.ACTIVE,
            agent_kind=kind,
            round_label=round_label,
            invocation_id=invocation_id,
            data=InvocationStartedData(system_prompt=system_prompt, user_prompt=user_prompt),
        )

    def after_agent(
        self, kind: str, round_label: str, *, result: Any = None, error: BaseException | None = None
    ) -> None:
        with self._condition:
            invocation_id = self._active_invocation
            self._active_invocation = None
            should_pause = self._pause_after_call
            if should_pause:
                self._pause_after_call = False
                self._paused = True

        self.record(
            EventType.INVOCATION_FINISHED,
            status=EventStatus.FAILED if error else EventStatus.COMPLETED,
            agent_kind=kind,
            round_label=round_label,
            invocation_id=invocation_id,
            data=InvocationFinishedData(
                result=json_value(result), error=repr(error) if error else None
            ),
        )
        self.record(
            EventType.PHASE_FINISHED,
            status=EventStatus.FAILED if error else EventStatus.COMPLETED,
            agent_kind=kind,
            round_label=round_label,
            invocation_id=invocation_id,
            data=PhaseData(phase=kind, attempt=_attempt_from_label(round_label)),
        )
        if should_pause:
            self.record(
                EventType.CONTROL,
                "/pause",
                status=EventStatus.CONSUMED,
                agent_kind=kind,
                round_label=round_label,
                invocation_id=invocation_id,
            )

    def status(self) -> str:
        with self._condition:
            state = "paused" if self._paused else self._run_status
            kind = self._current_kind or "starting"
            round_label = self._current_round or "no round yet"
        return f"{state} · {kind} · {round_label}"

    def finish(self, error: BaseException | None = None) -> None:
        with self._condition:
            self._run_status = "failed" if error else "completed"
            self._condition.notify_all()
        self.record(
            EventType.RUN_FAILED if error else EventType.RUN_FINISHED,
            repr(error) if error else "",
            status=EventStatus.FAILED if error else EventStatus.COMPLETED,
        )


def _attempt_from_label(round_label: str) -> int | None:
    match = re.search(r"retry-(\d+)", round_label)
    return int(match.group(1)) if match else None
