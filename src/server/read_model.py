"""Read-only queries over live and persisted run state."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from server.events import ConfigurationFailedData, EventStatus, EventType, RunEvent

if TYPE_CHECKING:
    from pathlib import Path

    from server.controller import ProjectRunState
    from server.events import EventData


class RunInspectionSource(Protocol):
    """The slice of the run integration adapter that inspector queries read.

    Declared here so this module does not import the adapter that constructs
    the inspector.
    """

    @property
    def project_run(self) -> ProjectRunState | None:
        """Return the attached canonical project run, if available."""
        ...

    @property
    def current_round(self) -> str | None:
        """Return the current controlled round label."""
        ...

    @property
    def log_dir(self) -> Path | None:
        """Return the attached wire-journal directory."""
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

    def read_events(
        self, after_sequence: int = 0, before_sequence: int | None = None
    ) -> list[RunEvent]:
        """Read canonical wire events within an optional cursor range."""
        ...

    def read_history_events(self) -> list[RunEvent]:
        """Read canonical wire history for inspector queries."""
        ...


@dataclass(frozen=True)
class _HistoryDocument:
    """Decoded read-only evidence from persisted state or a run log."""

    name: str
    text: str


class RunInspector:
    """Answer operator questions without mutating agent behavior."""

    def __init__(self, integration: RunInspectionSource) -> None:
        """Bind the read model to its run-inspection data source."""
        self.integration = integration

    def answer(self, question: str) -> str:
        """Answer a question using current status and persisted run history."""
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
            failed = self._latest_execution(status=EventStatus.FAILED)
            detail = (
                "Latest failed agent execution:\n" + failed
                if failed
                else self._search_latest(("judge", "fail", "feedback", "verdict"), "judge result")
            )
            answer = self._status_answer(question, detail)
        elif "judge" in query:
            judge = self._latest_execution(agent_kind="judge")
            detail = (
                "Latest judge execution:\n" + judge
                if judge
                else self._search_latest(("judge", "feedback", "verdict"), "judge result")
            )
            answer = self._status_answer(question, detail)
        elif any(word in query for word in ("benchmark", "performance", "metric", "latest result")):
            detail = self._search_latest(
                ("benchmark", "metric", "latency", "throughput"), "benchmark result"
            )
            answer = self._status_answer(question, detail)
        else:
            match = re.search(r"round\s+(\d+)", query)
            if match:
                answer = self._status_answer(question, self.round_detail(int(match.group(1))))
            elif "previous" in query or "last round" in query:
                current = re.search(
                    r"(?i)(?:round|iter(?:ation)?)\D*(\d+)", self.integration.current_round or ""
                )
                number = int(current.group(1)) if current else self._latest_round_number()
                if number:
                    answer = self._status_answer(question, self.round_detail(max(1, number - 1)))
        return answer

    def round_detail(self, number: int) -> str:
        """Return persisted history excerpts matching one round number."""
        pattern = re.compile(rf"(?i)\b(?:round(?:_number|_idx)?|iter(?:ation)?)\b\D*{number}\b")
        chunks = []
        for document in self._history_documents():
            lines = document.text.splitlines()
            indexes = [i for i, line in enumerate(lines) if pattern.search(line)]
            if indexes:
                start = max(0, indexes[-1] - 2)
                chunks.append(f"--- {document.name} ---\n" + "\n".join(lines[start : start + 80]))
        return "\n\n".join(chunks) or f"No persisted detail found for round {number}."

    def latest_run_log(self) -> Path | None:
        """Return the newest regular run log, if the integration has a log directory."""
        log_dir = self.integration.log_dir
        if log_dir is None:
            return None
        candidates = sorted(
            path for path in log_dir.glob("run-*.log") if path.is_file() and not path.is_symlink()
        )
        if candidates:
            return candidates[-1]
        return None

    def _status_answer(self, question: str, answer: str) -> str:
        self.integration.record(EventType.STATUS_QUERY, question)
        return answer

    def _history_documents(self) -> list[_HistoryDocument]:
        documents: list[_HistoryDocument] = []
        project_run = self.integration.project_run
        if project_run is not None:
            for snapshot in project_run.history_snapshots():
                documents.extend(
                    _HistoryDocument(
                        name=item.relative_path.name,
                        text=item.contents.decode("utf-8", errors="replace"),
                    )
                    for item in snapshot.files
                    if item.relative_path.suffix in {".json", ".jsonl", ".log", ".md", ".txt"}
                )
        latest = self.latest_run_log()
        if latest is not None:
            documents.append(
                _HistoryDocument(
                    name=latest.name,
                    text=latest.read_text(encoding="utf-8", errors="replace"),
                )
            )
        return documents

    def _search_latest(self, terms: tuple[str, ...], label: str) -> str:
        for document in reversed(self._history_documents()):
            lines = document.text.splitlines()
            hits = [
                i for i, line in enumerate(lines) if any(term in line.lower() for term in terms)
            ]
            if hits:
                start = max(0, hits[-1] - 8)
                return f"Latest {label} ({document.name}):\n" + "\n".join(
                    lines[start : hits[-1] + 12]
                )
        return f"No {label} has been persisted yet."

    def _latest_execution(
        self, *, status: EventStatus | None = None, agent_kind: str | None = None
    ) -> str | None:
        for event in reversed(self.integration.read_events()):
            if event.type is not EventType.AGENT_EXECUTION_FINISHED:
                continue
            if status is not None and event.status is not status:
                continue
            if agent_kind is not None and event.agent_kind != agent_kind:
                continue
            return self._format_event(event)
        return None

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

    def _latest_round_number(self) -> int | None:
        numbers = []
        pattern = re.compile(r"(?i)(?:round|iter(?:ation)?)\D*(\d+)")
        for document in self._history_documents():
            numbers.extend(int(match.group(1)) for match in pattern.finditer(document.text))
        return max(numbers) if numbers else None

    @staticmethod
    def _format_event(event: RunEvent) -> str:
        return json.dumps(event.model_dump(mode="json"), indent=2, ensure_ascii=False)
