"""Single-agent policy artifacts and human-readable progress memory."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.orchestration.single.prompts import render_pareto_frontier

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.hypothesis import OrchestratorPlan, ParetoArchiveView
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

    def note_plan(self, round_number: int, plan: OrchestratorPlan) -> None:
        """Append the selected hypothesis and task to the progress memory."""
        self._append(
            round_number,
            "Orchestrator plan",
            f"- hypothesis_id: {plan.hypothesis_id}\n"
            f"- hypothesis: {plan.hypothesis}\n"
            f"- task: {plan.task}\n"
            f"- pass criteria: {plan.pass_criteria}\n",
        )

    def note_continuation(self, round_number: int, hypothesis_id: str, task: str) -> None:
        """Record reuse of an active hypothesis without a designer turn."""
        self._append(
            round_number,
            "Active hypothesis continuation",
            f"- hypothesis_id: {hypothesis_id}\n- task: {task}\n",
        )

    def note_response(
        self, round_number: int, retry: int, response: SingleAgentRoundResponse
    ) -> None:
        """Append one combined implementation and self-review outcome."""
        self._append(
            round_number,
            f"Single-agent attempt {retry}",
            f"- verdict: {response.verdict.value}\n"
            f"- summary: {response.summary}\n"
            f"- feedback: {response.feedback or '(none)'}\n",
        )

    def note_turn_failed(self, round_number: int, retry: int, failure: TurnFailed) -> None:
        """Append one attempt whose agent returned no valid response."""
        self._append(
            round_number,
            f"Single-agent attempt {retry}",
            f"- verdict: no valid response\n- reason: {failure.reason}\n",
        )

    def note_evaluation(self, round_number: int, retry: int, detail: str) -> None:
        """Append the policy's official-evaluation decision or result."""
        self._append(round_number, f"Official evaluation attempt {retry}", detail)

    def _initialize(self) -> None:
        roadmap = self.roadmap / "index.md"
        roadmap.parent.mkdir(parents=True, exist_ok=True)
        if not roadmap.exists():
            roadmap.write_text("# Roadmap\n\nOwned and maintained by the orchestrator.\n")
        self.progress.mkdir(parents=True, exist_ok=True)
        readme = self.progress / "README.md"
        if not readme.exists():
            readme.write_text("# Progress\n\nOne audit file is written per round.\n")
        (self.artifact_root / "validation").mkdir(parents=True, exist_ok=True)

    def _append(self, round_number: int, title: str, body: str) -> None:
        path = self.progress / f"round-{round_number:04d}.md"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"## Round {round_number}: {title}\n{body.rstrip()}\n\n")


__all__ = ["SingleFiles"]
