"""Multi-agent policy artifacts and human-readable progress memory."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.orchestration.multi.prompts import (
    render_exhaustion_notice,
    render_pareto_frontier,
    render_regression_notice,
)
from vs_runtime.api import ValidationRecipeArtifact

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vibesys.orchestration.hypothesis import CarryOver, OrchestratorPlan, ParetoArchiveView
    from vibesys.orchestration.hypothesis.attempts import ImplementerReply
    from vibesys.orchestration.multi.contracts import (
        ImplementerResponse,
        JudgeResponse,
        PreRoundDecision,
    )
    from vibesys.orchestration.profilers import ProfilerSummary
    from vibesys.orchestration.structured_turn import TurnFailed


def _location(path: Path, workspace: Path, *, directory: bool = False) -> str:
    value = path.relative_to(workspace).as_posix()
    return f"{value}/" if directory else value


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _write_model(path: Path, value: BaseModel) -> None:
    _write_json(path, value.model_dump(mode="json"))


@dataclass(frozen=True, slots=True)
class MultiFiles:
    """Resolved policy-owned files inside one live run workspace."""

    workspace: Path
    roadmap: Path
    progress: Path

    @classmethod
    def open(cls, workspace: Path) -> MultiFiles:
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
        """Return the roadmap index path shown to agents."""
        return _location(self.roadmap / "index.md", self.workspace)

    @property
    def progress_location(self) -> str:
        """Return the progress ledger path shown to agents."""
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
    def validation_root(self) -> Path:
        """Return the policy's local-validation artifact directory."""
        return self.artifact_root / "validation"

    @property
    def validation_location(self) -> str:
        """Return the validation directory shown to agents."""
        return _location(self.validation_root, self.workspace, directory=True)

    @property
    def validation_schema_location(self) -> str:
        """Return the candidate-authored recipe contract path."""
        return _location(self.validation_root / "recipe-schema.json", self.workspace)

    def validation_report_location(self, round_number: int, retry: int) -> str:
        """Return the semantic runtime report path for one attempt."""
        return _location(
            self.validation_root / f"round-{round_number:04d}-attempt-{retry:02d}.json",
            self.workspace,
        )

    def profiler_location(self, round_number: int) -> str:
        """Create and return one profiler's bounded evidence directory."""
        path = self.artifact_root / "profiles" / f"round-{round_number:04d}"
        path.mkdir(parents=True, exist_ok=True)
        return _location(path, self.workspace, directory=True)

    def write_plan(self, round_number: int, plan: OrchestratorPlan) -> str:
        """Persist the exact designer plan and return its visible path."""
        path = self.artifact_root / "plans" / f"round-{round_number:04d}.json"
        _write_model(path, plan)
        return _location(path, self.workspace)

    def write_implementer(
        self,
        round_number: int,
        retry: int,
        response: ImplementerReply,
    ) -> str:
        """Persist untrusted implementer claims for independent review."""
        path = (
            self.artifact_root
            / "evidence"
            / f"round-{round_number:04d}-attempt-{retry:02d}-implementer.json"
        )
        _write_json(path, response.model_dump(mode="json"))
        return _location(path, self.workspace)

    def prior_implementer_locations(self, round_number: int) -> tuple[str, ...]:
        """Return retained same-round implementer reports in attempt order."""
        root = self.artifact_root / "evidence"
        return tuple(
            _location(path, self.workspace)
            for path in sorted(root.glob(f"round-{round_number:04d}-attempt-*-implementer.json"))
        )

    def write_pareto(self, archive: ParetoArchiveView) -> None:
        """Replace the derived Pareto archive."""
        self.pareto_path.parent.mkdir(parents=True, exist_ok=True)
        self.pareto_path.write_text(render_pareto_frontier(archive))

    def note_carry(self, round_number: int, carry: CarryOver) -> None:
        """Append the carried regression and exhausted-review notices the designer reads."""
        if carry.regression is not None:
            self._append(
                round_number,
                "Regression or terminal-workspace notice",
                render_regression_notice(carry.regression),
            )
        if carry.exhaustion is not None:
            self._append(
                round_number,
                "Exhausted-review feedback",
                render_exhaustion_notice(carry.exhaustion),
            )

    def note_pre_round(self, round_number: int, decision: PreRoundDecision) -> None:
        """Append one profiling decision to campaign memory."""
        self._append(
            round_number,
            "Pre-round profile decision",
            f"- profile: {decision.need_profile}\n"
            f"- focus: {decision.profile_focus or '(none)'}\n"
            f"- reasoning: {decision.reasoning}\n",
        )

    def note_profile(self, round_number: int, summary: ProfilerSummary) -> None:
        """Append specialist profile evidence to campaign memory."""
        self._append(
            round_number,
            "Profiler summary",
            f"- analysis: {summary.analysis}\n"
            f"- bottlenecks: {summary.bottlenecks}\n"
            f"- suggestions: {summary.suggestions}\n",
        )

    def note_plan(self, round_number: int, plan: OrchestratorPlan) -> None:
        """Append one selected hypothesis to campaign memory."""
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

    def note_implementation(
        self,
        round_number: int,
        retry: int,
        response: ImplementerResponse,
    ) -> None:
        """Append one implementer outcome to campaign memory."""
        self._append(
            round_number,
            f"Implementer attempt {retry}",
            f"- outcome: {response.hypothesis_outcome.value}\n"
            f"- disposition: {response.candidate_disposition.value}\n"
            f"- summary: {response.summary}\n",
        )

    def note_implementation_failed(
        self,
        round_number: int,
        retry: int,
        failure: TurnFailed,
    ) -> None:
        """Append an implementer attempt that returned no valid response."""
        self._append(
            round_number,
            f"Implementer attempt {retry}",
            f"- outcome: no valid response\n- reason: {failure.reason}\n",
        )

    def note_judge(self, round_number: int, retry: int, response: JudgeResponse) -> None:
        """Append one independent verdict to campaign memory."""
        self._append(
            round_number,
            f"Judge attempt {retry}",
            f"- verdict: {response.verdict.value}\n"
            f"- feedback: {response.feedback or '(none)'}\n"
            f"- analysis: {response.analysis}\n",
        )

    def note_review_skipped(self, round_number: int, outcome: str) -> None:
        """Record a sparse-review deferral."""
        self._append(
            round_number,
            "Judge deferred",
            f"- outcome: {outcome}\n- reason: sparse review policy\n",
        )

    def note_evaluation(self, round_number: int, retry: int, detail: str) -> None:
        """Append a local or official evaluation decision."""
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
        self.validation_root.mkdir(parents=True, exist_ok=True)
        schema_path = self.validation_root / "recipe-schema.json"
        if not schema_path.exists():
            _write_json(schema_path, ValidationRecipeArtifact.model_json_schema())

    def _append(self, round_number: int, title: str, body: str) -> None:
        path = self.progress / f"round-{round_number:04d}.md"
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"## Round {round_number}: {title}\n{body.rstrip()}\n\n")


__all__ = ["MultiFiles"]
