"""Single-agent policy artifacts and human-readable progress memory."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.orchestration.progress import CarriedEntries, ProgressLog
from vibesys.orchestration.single.prompts import render_pareto_frontier, render_progress

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.hypothesis import CarryOver, OrchestratorPlan, ParetoArchiveView
    from vibesys.orchestration.profilers import ProfilerSummary
    from vibesys.orchestration.progress import ProgressEntry
    from vibesys.orchestration.single.models import SingleAgentRoundResponse
    from vibesys.orchestration.structured_turn import TurnFailed


def _location(path: Path, workspace: Path, *, directory: bool = False) -> str:
    value = path.relative_to(workspace).as_posix()
    return f"{value}/" if directory else value


def _write_model(path: Path, value: BaseModel) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value.model_dump(mode="json"), stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


@dataclass(frozen=True, slots=True)
class SingleFiles:
    """Resolved policy-owned files inside one live run workspace."""

    workspace: Path
    roadmap: Path
    progress: Path

    @classmethod
    def open(cls, workspace: Path) -> SingleFiles:
        """Resolve and initialize the policy's canonical memory directories."""
        result = cls(
            workspace=workspace,
            roadmap=workspace / "roadmap",
            progress=workspace / "progress",
        )
        result._initialize()
        return result

    @property
    def roadmap_location(self) -> str:
        """Return the roadmap path shown to agents."""
        return _location(self.roadmap / "index.md", self.workspace)

    @property
    def progress_location(self) -> str:
        """Return the progress path shown to agents."""
        return _location(self.progress, self.workspace, directory=True)

    @property
    def pareto_path(self) -> Path:
        """Return the derived Pareto archive path."""
        return self.progress / "pareto-frontier.md"

    @property
    def pareto_location(self) -> str:
        """Return the Pareto archive path shown to agents."""
        return _location(self.pareto_path, self.workspace)

    @property
    def artifact_root(self) -> Path:
        """Return the root for structured policy handoff files."""
        return self.progress

    @property
    def validation_location(self) -> str:
        """Return the validation artifact directory shown to agents."""
        return _location(self.artifact_root / "validation", self.workspace, directory=True)

    def write_plan(self, round_number: int, plan: OrchestratorPlan) -> str:
        """Persist the exact designer plan and return its agent-visible path."""
        path = self.artifact_root / "plans" / f"round-{round_number:04d}.json"
        _write_model(path, plan)
        return _location(path, self.workspace)

    def write_pareto(self, archive: ParetoArchiveView) -> None:
        """Replace the derived Pareto archive."""
        self.pareto_path.parent.mkdir(parents=True, exist_ok=True)
        self.pareto_path.write_text(render_pareto_frontier(archive))

    def note_carry(self, round_number: int, carry: CarryOver) -> CarriedEntries:
        """Append the carried regression and exhausted-review notices the designer reads."""
        return CarriedEntries(
            regression=(
                self._section(round_number, "regression", regression_info=carry.regression)
                if carry.regression is not None
                else None
            ),
            exhaustion=(
                self._section(round_number, "exhaustion", exhaustion_info=carry.exhaustion)
                if carry.exhaustion is not None
                else None
            ),
        )

    def note_profile(self, round_number: int, summary: ProfilerSummary) -> ProgressEntry:
        """Append the profile evidence the designer is pointed at."""
        return self._section(round_number, "profile", summary=summary)

    def note_plan(self, round_number: int, plan: OrchestratorPlan) -> None:
        """Append the selected hypothesis and task to the progress memory."""
        self._section(round_number, "plan", plan=plan)

    def note_continuation(self, round_number: int, hypothesis_id: str, task: str) -> None:
        """Record reuse of an active hypothesis without a designer turn."""
        self._section(round_number, "continuation", hypothesis_id=hypothesis_id, task=task)

    def note_response(
        self, round_number: int, retry: int, response: SingleAgentRoundResponse
    ) -> None:
        """Append one combined implementation and self-review outcome."""
        self._section(round_number, "single_response", retry=retry, response=response)

    def note_turn_failed(self, round_number: int, retry: int, failure: TurnFailed) -> None:
        """Append one attempt whose agent returned no valid response."""
        self._section(round_number, "single_turn_failed", retry=retry, reason=failure.reason)

    def note_evaluation_deferred(self, round_number: int, retry: int) -> None:
        """Record that the official-evaluation cadence was not due."""
        self._section(round_number, "evaluation_deferred", retry=retry)

    def note_evaluation_passed(self, round_number: int, retry: int, reason: str | None) -> None:
        """Record a passed official evaluation and why it ran."""
        self._section(round_number, "evaluation_passed", retry=retry, reason=reason)

    def note_evaluation_failed(self, round_number: int, retry: int, feedback: str) -> None:
        """Record a failed official evaluation and its feedback."""
        self._section(round_number, "evaluation_failed", retry=retry, feedback=feedback)

    def _section(self, round_number: int, section: str, **context: object) -> ProgressEntry:
        log = ProgressLog(self.workspace, self.progress)
        return log.append(
            round_number, render_progress(section, round_number=round_number, **context)
        )

    def _initialize(self) -> None:
        roadmap = self.roadmap / "index.md"
        roadmap.parent.mkdir(parents=True, exist_ok=True)
        if not roadmap.exists():
            roadmap.write_text(render_progress("roadmap"))
        self.progress.mkdir(parents=True, exist_ok=True)
        readme = self.progress / "README.md"
        if not readme.exists():
            readme.write_text(render_progress("readme"))
        (self.artifact_root / "validation").mkdir(parents=True, exist_ok=True)


__all__ = ["SingleFiles"]
