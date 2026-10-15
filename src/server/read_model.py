"""Read-only queries over live and persisted run state."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol

from server.events import (
    AgentExecutionFinishedData,
    BenchmarkResultData,
    ConfigurationFailedData,
    EventStatus,
    EventType,
    GateFinishedData,
    GateKind,
    JudgeResultData,
    RoundFinishedData,
    RunEvent,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from server.events import EventData

_ROUND_NUMBER = re.compile(r"(?i)(?:round|iter(?:ation)?)\D*(\d+)")
"""How a round label names a round: ``round-3``, ``round 3``, ``iteration 3``."""

_DETAIL_EVENT_TYPES = frozenset(
    {
        EventType.AGENT_EXECUTION_FINISHED,
        EventType.GATE_FINISHED,
        EventType.JUDGE_RESULT,
        EventType.BENCHMARK_RESULT,
        EventType.ROUND_FINISHED,
    }
)
"""The typed outcomes a round's detail lists, in the order they were recorded."""


class RunInspectionSource(Protocol):
    """The slice of the run integration adapter that inspector queries read.

    Declared here so this module does not import the adapter that constructs
    the inspector. Every fact comes from typed events; the inspector never
    reads the human-facing run log.
    """

    @property
    def current_round(self) -> str | None:
        """Return the current controlled round label."""
        ...

    def status(self) -> str:
        """Return a compact human-readable run status."""
        ...

    def record(
        self,
        event_type: EventType,
        text: str = "",
        *,
        data: EventData | None = None,
        **fields: object,
    ) -> RunEvent:
        """Record a server-only wire event."""
        ...

    def read_history_events(self) -> list[RunEvent]:
        """Read canonical wire history for inspector queries."""
        ...


def _round_number(label: str | None) -> int | None:
    match = _ROUND_NUMBER.search(label or "")
    return int(match.group(1)) if match else None


def _clip(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + "..."


def _where(event: RunEvent) -> str:
    parts = [part for part in (event.round_label, event.agent_kind) if part]
    return f" ({', '.join(parts)})" if parts else ""


def _diagnostic_lines(event: RunEvent) -> list[str]:
    diagnostic = event.diagnostic
    if diagnostic is None:
        return []
    lines = [f"Diagnostic: {_clip(diagnostic.summary)}"]
    if diagnostic.detail:
        lines.append(_clip(diagnostic.detail))
    return lines


def _format_execution(event: RunEvent, data: AgentExecutionFinishedData) -> str:
    lines = [f"Agent execution {event.status or 'finished'}{_where(event)}"]
    if data.error:
        lines.append(f"Error: {_clip(data.error)}")
    elif event.text:
        lines.append(_clip(event.text))
    return "\n".join([*lines, *_diagnostic_lines(event)])


def _format_gate(event: RunEvent, data: GateFinishedData) -> str:
    name = data.gate.value + (f" {data.recipe}" if data.recipe else "")
    lines = [f"Gate {name} {event.status or 'finished'}{_where(event)}"]
    if data.metric is not None and data.value is not None:
        lines.append(f"Metric: {data.metric} = {data.value} {data.unit or ''}".rstrip())
    if data.output_tail:
        lines.append("Output tail:\n" + _clip(data.output_tail))
    return "\n".join([*lines, *_diagnostic_lines(event)])


def _format_judge(event: RunEvent, data: JudgeResultData) -> str:
    lines = [f"Judge verdict: {data.verdict} (attempt {data.attempt}){_where(event)}"]
    if data.feedback:
        lines.append("Feedback: " + _clip(data.feedback))
    return "\n".join(lines)


def _format_benchmark(event: RunEvent, data: BenchmarkResultData) -> str:
    return f"Benchmark {data.metric}: {data.value} {data.unit}{_where(event)}".rstrip()


def _format_round(event: RunEvent, data: RoundFinishedData) -> str:
    line = f"Round finished{_where(event)}: {data.attempts} attempt(s), judge {data.judge_verdict}"
    if data.perf_metric is not None:
        line += f", perf {data.perf_metric} {data.perf_unit or ''}".rstrip()
    return line


def _format_typed(event: RunEvent) -> str | None:
    """Render one typed outcome event, or None when it is not one the inspector reports."""
    data = event.data
    if isinstance(data, AgentExecutionFinishedData):
        return _format_execution(event, data)
    if isinstance(data, GateFinishedData):
        return _format_gate(event, data)
    if isinstance(data, JudgeResultData):
        return _format_judge(event, data)
    if isinstance(data, BenchmarkResultData):
        return _format_benchmark(event, data)
    if isinstance(data, RoundFinishedData):
        return _format_round(event, data)
    return None


def _is_failure(event: RunEvent) -> bool:
    return event.status is EventStatus.FAILED and event.type in {
        EventType.AGENT_EXECUTION_FINISHED,
        EventType.GATE_FINISHED,
    }


def _is_judge(event: RunEvent) -> bool:
    return event.type is EventType.JUDGE_RESULT


def _is_benchmark(event: RunEvent) -> bool:
    if event.type is EventType.BENCHMARK_RESULT:
        return True
    return (
        isinstance(event.data, GateFinishedData)
        and event.data.gate is GateKind.BENCHMARK
        and event.data.metric is not None
    )


class RunInspector:
    """Answer operator questions from typed run events without mutating agent behavior."""

    def __init__(self, integration: RunInspectionSource) -> None:
        """Bind the read model to its run-inspection data source."""
        self.integration = integration

    def answer(self, question: str) -> str:
        """Answer a question using current status and the typed event history."""
        configuration_failure = self._latest_configuration_failure()
        if configuration_failure is not None:
            return self._status_answer(question, configuration_failure)
        query = question.lower()
        answer = self._matched_query_answer(question, query)
        if answer is not None:
            return answer
        # Names only what this read-only matcher can actually answer. It must
        # not advertise slash commands: this string is shown in the experiment
        # chat, and the operator would have to leave it to run one.
        return (
            f"{self.integration.status()}. This summary matches on keywords, so it answers "
            "questions about a round, a failure, the judge, or a benchmark."
        )

    def _matched_query_answer(self, question: str, query: str) -> str | None:
        """Resolve recognized query intents in their established priority order."""
        answer: str | None = None
        if any(word in query for word in ("doing", "current", "status", "now")):
            answer = self._status_answer(question, self.integration.status())
        elif any(word in query for word in ("failed", "failure", "why")):
            detail = self._latest(
                _is_failure,
                "Latest failure",
                "No failed agent execution or gate has been recorded yet.",
            )
            answer = self._status_answer(question, detail)
        elif "judge" in query:
            detail = self._latest(
                _is_judge, "Latest judge result", "No judge result has been recorded yet."
            )
            answer = self._status_answer(question, detail)
        elif any(word in query for word in ("benchmark", "performance", "metric", "latest result")):
            detail = self._latest(
                _is_benchmark,
                "Latest benchmark result",
                "No benchmark result has been recorded yet.",
            )
            answer = self._status_answer(question, detail)
        else:
            match = re.search(r"round\s+(\d+)", query)
            if match:
                answer = self._status_answer(question, self.round_detail(int(match.group(1))))
            elif "previous" in query or "last round" in query:
                number = self._previous_round_number()
                detail = (
                    "No previous round has been recorded yet."
                    if number is None
                    else self.round_detail(number)
                )
                answer = self._status_answer(question, detail)
        return answer

    def round_detail(self, number: int) -> str:
        """Return the typed outcomes recorded for one round number."""
        lines = [
            text
            for event in self.integration.read_history_events()
            if event.type in _DETAIL_EVENT_TYPES
            and _round_number(event.round_label) == number
            and (text := _format_typed(event)) is not None
        ]
        return "\n\n".join(lines) or f"No recorded events found for round {number}."

    def _status_answer(self, question: str, answer: str) -> str:
        self.integration.record(EventType.STATUS_QUERY, question)
        return answer

    def _latest(self, wanted: Callable[[RunEvent], bool], title: str, missing: str) -> str:
        for event in reversed(self.integration.read_history_events()):
            if wanted(event) and (text := _format_typed(event)) is not None:
                return f"{title}:\n{text}"
        return missing

    def _latest_configuration_failure(self) -> str | None:
        for event in reversed(self.integration.read_history_events()):
            if event.type is not EventType.CONFIGURATION_FAILED:
                continue
            data = event.data
            if not isinstance(data, ConfigurationFailedData):
                continue
            answer = f"Experiment configuration failed during {data.stage}: {data.message}"
            if data.usage:
                answer += f"\n\n{data.usage}"
            return answer
        return None

    def _previous_round_number(self) -> int | None:
        """The round before the current one, else the latest round that finished."""
        current = _round_number(self.integration.current_round)
        if current is not None:
            return current - 1 if current > 1 else None
        finished = [
            number
            for event in self.integration.read_history_events()
            if event.type is EventType.ROUND_FINISHED
            and (number := _round_number(event.round_label)) is not None
        ]
        return max(finished) if finished else None
