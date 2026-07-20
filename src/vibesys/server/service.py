"""Presentation-neutral supervision application service."""

from __future__ import annotations

import json

from vibesys.server.events import EventType, RunEvent
from vibesys.server.inspector import RunInspector
from vibesys.server.protocol import (
    ChatQuery,
    ChatResult,
    CommandAck,
    EventsQuery,
    HistoryQuery,
    PauseCommand,
    PerformanceQuery,
    PerformanceRound,
    ProtocolRequest,
    Response,
    ResumeCommand,
    RunSnapshot,
    SnapshotQuery,
)
from vibesys.server.supervisor import RunSupervisor


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
            sequence = self.supervisor.snapshot().sequence
            answer = self.supervisor.chat(request.text)
            return Response(
                request_id=request.request_id,
                chat=ChatResult(question=request.text, answer=answer),
                events=self.supervisor.read_events(sequence),
            )
        if isinstance(request, HistoryQuery):
            self.supervisor.record(EventType.STATUS_QUERY, "/history")
            return Response(request_id=request.request_id, events=self.history_events())
        if isinstance(request, PerformanceQuery):
            self.supervisor.record(EventType.STATUS_QUERY, "/perf")
            return Response(request_id=request.request_id, performance=self.performance_rounds())
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

    def performance_rounds(self) -> list[PerformanceRound]:
        log_dir = self.supervisor.log_dir
        if log_dir is None:
            return []
        rounds_path = log_dir / "rounds.json"
        if not rounds_path.is_file():
            return []
        rounds: list[PerformanceRound] = []
        for item in json.loads(rounds_path.read_text(encoding="utf-8")):
            metric = item.get("perf_metric")
            unit = item.get("perf_unit")
            if not isinstance(metric, int | float) or not isinstance(unit, str) or not unit:
                continue
            rounds.append(
                PerformanceRound(
                    round=int(item["round"]),
                    perf_metric=float(metric),
                    perf_unit=unit,
                    passed=bool(item.get("passed", False)),
                    profile_skipped=bool(item.get("profile_skipped", False)),
                )
            )
        return rounds

    def wait_for_events(self, after_sequence: int, timeout: float | None = None) -> list[RunEvent]:
        return self.supervisor.wait_for_events(after_sequence, timeout)
