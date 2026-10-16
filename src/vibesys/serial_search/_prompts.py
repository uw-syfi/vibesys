"""The designer's request, as typed data for the prompt renderer.

The search never builds prompt text. `PlanPrompt` mirrors the variables of `plan.j2`; when the
ledger refuses the designer's plan, the same request is asked again as a `PlanCorrection` that
names why.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Literal

from vibesys.serial_search._plan_view import LastProfile, PlanEvidence, plan_evidence
from vibesys.serial_search._roles import ORCHESTRATOR
from vs_core.api.search import (
    Correction,
    InvocationRef,
    RevisionRef,
    RolePromptContext,
    SchemaRef,
)

if TYPE_CHECKING:
    from vibesys.hypothesis import HypothesisConfig, PlanningContext, rounds
    from vibesys.profile_focus import FocusView
    from vs_issue_tracker.api import RoadmapItem


class PlanTemplate(StrEnum):
    """The designer's contexts: the request and its correction."""

    PLAN = "plan"
    PLAN_CORRECTION = "plan_correction"


class _Plan(PlanEvidence, RolePromptContext):
    """What a designer request says; the request and its correction share it."""

    reply_schema: ClassVar[SchemaRef | None] = ORCHESTRATOR.reply_schema

    base_revision: RevisionRef
    problem: str | None = None
    """Why the earlier plan was not accepted; None for the first request."""


class PlanPrompt(_Plan):
    """One new-hypothesis planning request."""

    template: Literal[PlanTemplate.PLAN] = PlanTemplate.PLAN


class PlanCorrection(_Plan, Correction):
    """The planning request again, as the correction of the earlier turn the ledger refused."""

    template: Literal[PlanTemplate.PLAN_CORRECTION] = PlanTemplate.PLAN_CORRECTION


def plan_prompt(  # noqa: PLR0913  # LW-641208 [PLR0913]; the request is built from independent facts of the run that no one record holds.
    hypotheses: HypothesisConfig,
    ledger: rounds.Ledger,
    planning: PlanningContext,
    focus: FocusView | None,
    *,
    base: RevisionRef,
    benchmark_enabled: bool,
    last_profile: LastProfile | None,
    roadmap: tuple[RoadmapItem, ...],
) -> PlanPrompt:
    """The new-hypothesis request: history, the frontier, notices, the last profile, the roadmap."""
    evidence = plan_evidence(hypotheses, ledger, planning, focus).model_copy(
        update={
            "benchmark_enabled": benchmark_enabled,
            "last_profile": last_profile,
            "roadmap": roadmap,
        }
    )
    return PlanPrompt(
        **{name: getattr(evidence, name) for name in PlanEvidence.model_fields},
        base_revision=base,
    )


def correction_of(prompt: PlanPrompt, of: InvocationRef, problem: str) -> PlanCorrection:
    """``prompt`` asked again as the correction of the turn ``of``, which ``problem`` names."""
    fields = {name: getattr(prompt, name) for name in PlanEvidence.model_fields}
    return PlanCorrection(**fields, base_revision=prompt.base_revision, of=of, problem=problem)
