"""Presentation-neutral supervision application service."""

from __future__ import annotations

from vibe_sys.server.events import EventType, RunEvent
from vibe_sys.server.inspector import RunInspector
from vibe_sys.server.protocol import (
    ChatQuery,
    ChatResult,
    CommandAck,
    EventsQuery,
    HistoryQuery,
    PauseCommand,
    ProtocolRequest,
    Response,
    ResumeCommand,
    RunSnapshot,
    SnapshotQuery,
)
from vibe_sys.server.supervisor import RunSupervisor


class SupervisionService:
    """Authoritative message API consumed by every presentation client."""

    def __init__(self, supervisor: RunSupervisor):
        self.supervisor = supervisor
        self.inspector = RunInspector(supervisor)

    def execute(self, request: ProtocolRequest) -> Response:
        if isinstance(request, PauseCommand):
            self.supervisor.pause_after_call()
            return Response(
                request_id=request.request_id,
                ack=CommandAck(action="pause", status="pending"),
            )
        if isinstance(request, ResumeCommand):
            self.supervisor.resume()
            return Response(
                request_id=request.request_id,
                ack=CommandAck(action="resume", status="consumed"),
            )
        if isinstance(request, ChatQuery):
            answer = self.supervisor.chat(request.text)
            return Response(
                request_id=request.request_id,
                chat=ChatResult(question=request.text, answer=answer),
            )
        if isinstance(request, HistoryQuery):
            self.supervisor.record(EventType.STATUS_QUERY, "/history")
            return Response(request_id=request.request_id, events=self.history_events())
        if isinstance(request, SnapshotQuery):
            return Response(request_id=request.request_id, snapshot=self.snapshot())
        if isinstance(request, EventsQuery):
            timeout = request.timeout_ms / 1000 if request.timeout_ms else None
            events = (
                self.wait_for_events(request.after_sequence, timeout)
                if timeout is not None
                else self.events(request.after_sequence)
            )
            return Response(request_id=request.request_id, events=events)
        raise TypeError(f"Unsupported protocol request: {type(request).__name__}")

    def snapshot(self) -> RunSnapshot:
        return self.supervisor.snapshot()

    def events(self, after_sequence: int = 0) -> list[RunEvent]:
        return self.supervisor.read_events(after_sequence)

    def history_events(self) -> list[RunEvent]:
        return self.supervisor.read_history_events()

    def wait_for_events(self, after_sequence: int, timeout: float | None = None) -> list[RunEvent]:
        return self.supervisor.wait_for_events(after_sequence, timeout)
