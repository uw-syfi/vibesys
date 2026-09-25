"""Mutator role family: evolve's LLM-as-mutation-operator.

Evolve reuses the ``implementer`` agent kind for the mutation operator (same
backend/model config lookup as every other strategy's implementer), so
``CANDIDATE_MUTATOR.id == "implementer"``.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vibesys.evaluators.metrics import Objective
from vibesys.runtime import Reuse, Role, Writes
from vibesys.search.population.models import Individual


class MutatorContext(BaseModel):
    """Context for evolve's mutator role (``mutator_prompt.j2``).

    ``objectives`` gates an unused Pareto-frontier section: no caller
    populates it today (the mutator always mutates one lineage at a time),
    so it stays ``None`` in practice, but the template already reads it and
    the field belongs here regardless.
    """

    model_config = ConfigDict(frozen=True)

    accuracy_command: str | None
    benchmark_command: str | None
    domain_implementer: str
    failed_lessons: list[str]
    inspirations: list[Individual]
    interface: str
    is_cold_start: bool
    modality: str | None
    num_failed_attempts: int
    objective: str
    objectives: list[Objective] | None
    parent: Individual | None
    reference_path: str
    repair_seed: bool
    runtime_notes: str


class MutatorResponse(BaseModel):
    """Structured response from the Mutator agent.

    The Mutator is the LLM-as-mutation-operator: given a parent program
    + inspiration peers, it edits the workspace files in place and
    returns a short rationale. The rationale is recorded on the
    offspring's ``Individual`` so future rounds can read it as part of
    population history.
    """

    summary: str = Field(description="Short description of the change made to the parent.")
    hypothesis: str = Field(
        description="Why this change is expected to improve the headline metric.",
    )
    expected_behavior: str = Field(
        description="Observable change a reviewer should expect (e.g. 'CUDA graph replays > 0', 'tok/s improves vs parent')."
    )


def _fallback_mutator() -> MutatorResponse:
    return MutatorResponse(
        summary="Mutator produced no structured response.",
        hypothesis="unknown",
        expected_behavior="unknown",
    )


CANDIDATE_MUTATOR = Role(
    id="implementer",
    template="loops/evolve/mutator_prompt.j2",
    reply=MutatorResponse,
    fallback=_fallback_mutator,
    context=MutatorContext,
    access=Writes(),
    session=Reuse(),
    message=(
        "Edit the workspace to produce an offspring of the parent. "
        "Then return one JSON object matching the schema above."
    ),
)

ALL_ROLES = (CANDIDATE_MUTATOR,)
