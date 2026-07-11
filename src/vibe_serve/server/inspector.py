"""Read-only queries over live and persisted run state."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from vibe_serve.server.events import ArtifactData, EventStatus, EventType, RunEvent
from vibe_serve.server.protocol import ArtifactResult, RoundResult, TextBlock

if TYPE_CHECKING:
    from vibe_serve.server.supervisor import RunSupervisor


class RunInspector:
    """Answer operator questions without mutating agent behavior."""

    def __init__(self, supervisor: RunSupervisor):
        self.supervisor = supervisor

    def answer(self, question: str) -> str:
        query = question.lower()
        if any(word in query for word in ("doing", "current", "status", "now")):
            return self._status_answer(question, self.supervisor.status())
        if any(word in query for word in ("failed", "failure", "why")):
            failed = self._latest_invocation(status=EventStatus.FAILED)
            answer = (
                "Latest failed agent invocation:\n" + failed
                if failed
                else self._search_latest(("judge", "fail", "feedback", "verdict"), "judge result")
            )
            return self._status_answer(question, answer)
        if "judge" in query:
            judge = self._latest_invocation(agent_kind="judge")
            answer = (
                "Latest judge invocation:\n" + judge
                if judge
                else self._search_latest(("judge", "feedback", "verdict"), "judge result")
            )
            return self._status_answer(question, answer)
        if any(word in query for word in ("benchmark", "performance", "metric", "latest result")):
            return self._status_answer(
                question,
                self._search_latest(
                    ("benchmark", "metric", "latency", "throughput"), "benchmark result"
                ),
            )
        match = re.search(r"round\s+(\d+)", query)
        if match:
            return self._status_answer(question, self.round_detail(int(match.group(1))))
        if "previous" in query or "last round" in query:
            current = re.search(
                r"(?i)(?:round|iter(?:ation)?)\D*(\d+)", self.supervisor.current_round or ""
            )
            number = int(current.group(1)) if current else self._latest_round_number()
            if number:
                return self._status_answer(question, self.round_detail(max(1, number - 1)))
        return (
            f"{self.supervisor.status()}. Ask about a round, failure, judge, or benchmark; "
            "use /history for the timeline and /show PATH for an artifact."
        )

    def timeline(self) -> str:
        events = self.supervisor.read_events()
        if not events:
            return "No TUI events have been recorded yet."
        lines = []
        for event in events[-200:]:
            target = " / ".join(filter(None, (event.round_label, event.agent_kind)))
            description = event.text or event.status or ""
            invocation = f" [{event.invocation_id[:8]}]" if event.invocation_id else ""
            lines.append(
                f"{event.timestamp:%H:%M:%S} {event.type}{invocation} {target} {description}".rstrip()
            )
        return "\n".join(lines)

    def invocation_detail(self, invocation_prefix: str) -> str:
        matches = self.invocation_events(invocation_prefix)
        if not matches:
            return f"No invocation matching {invocation_prefix!r}."
        invocation_ids = {event.invocation_id for event in matches}
        if len(invocation_ids) > 1:
            return f"Invocation prefix {invocation_prefix!r} is ambiguous."
        return "\n\n".join(self._format_event(event) for event in matches)

    def invocation_events(self, invocation_prefix: str) -> list[RunEvent]:
        return [
            event
            for event in self.supervisor.read_events()
            if event.invocation_id and event.invocation_id.startswith(invocation_prefix)
        ]

    def round_detail(self, number: int) -> str:
        result = self.round_result(number)
        return (
            "\n\n".join(f"--- {block.source} ---\n{block.content}" for block in result.blocks)
            or f"No persisted detail found for round {number}."
        )

    def round_result(self, number: int) -> RoundResult:
        pattern = re.compile(rf"(?i)(round|iter(?:ation)?)\D*{number}\b")
        blocks = []
        for path in self._history_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            indexes = [i for i, line in enumerate(lines) if pattern.search(line)]
            if indexes:
                start = max(0, indexes[-1] - 2)
                blocks.append(
                    TextBlock(source=path.name, content="\n".join(lines[start : start + 80]))
                )
        return RoundResult(round_number=number, blocks=blocks)

    def show_artifact(self, requested: str) -> str:
        result = self.artifact_result(requested)
        return f"--- {result.path} ---\n{result.content}"

    def artifact_result(self, requested: str) -> ArtifactResult:
        log_dir = self.supervisor.log_dir
        if log_dir is None:
            raise ValueError("The run directory is not ready yet.")
        root = log_dir.parent.resolve()
        path = (root / requested).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            raise ValueError("Artifacts must be inside the experiment directory.") from None
        if not path.is_file():
            raise ValueError(f"Artifact not found: {requested}")
        self.supervisor.record(
            EventType.STATUS_QUERY,
            f"/show {requested}",
            data=ArtifactData(path=str(path)),
        )
        text = path.read_text(encoding="utf-8", errors="replace")
        return ArtifactResult(path=str(path.relative_to(root)), content=text[-40_000:])

    def latest_run_log(self) -> Path | None:
        log_dir = self.supervisor.log_dir
        if log_dir is None:
            return None
        candidates = sorted(log_dir.glob("run-*.log"))
        if candidates:
            return candidates[-1]
        return None

    def _status_answer(self, question: str, answer: str) -> str:
        self.supervisor.record(EventType.STATUS_QUERY, question)
        return answer

    def _history_files(self) -> list[Path]:
        log_dir = self.supervisor.log_dir
        if log_dir is None:
            return []
        names = ("progress.md", "rounds.json", "state.json", "perf_metrics.json")
        files = [log_dir / name for name in names if (log_dir / name).is_file()]
        latest = self.latest_run_log()
        return files + ([latest] if latest else [])

    def _search_latest(self, terms: tuple[str, ...], label: str) -> str:
        for path in reversed(self._history_files()):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            hits = [
                i for i, line in enumerate(lines) if any(term in line.lower() for term in terms)
            ]
            if hits:
                start = max(0, hits[-1] - 8)
                return f"Latest {label} ({path.name}):\n" + "\n".join(lines[start : hits[-1] + 12])
        return f"No {label} has been persisted yet."

    def _latest_invocation(
        self, *, status: EventStatus | None = None, agent_kind: str | None = None
    ) -> str | None:
        for event in reversed(self.supervisor.read_events()):
            if event.type is not EventType.INVOCATION_FINISHED:
                continue
            if status is not None and event.status is not status:
                continue
            if agent_kind is not None and event.agent_kind != agent_kind:
                continue
            return self._format_event(event)
        return None

    def _latest_round_number(self) -> int | None:
        numbers = []
        pattern = re.compile(r"(?i)(?:round|iter(?:ation)?)\D*(\d+)")
        for path in self._history_files():
            text = path.read_text(encoding="utf-8", errors="replace")
            numbers.extend(int(match.group(1)) for match in pattern.finditer(text))
        return max(numbers) if numbers else None

    @staticmethod
    def _format_event(event: RunEvent) -> str:
        return json.dumps(event.model_dump(mode="json"), indent=2, ensure_ascii=False)
