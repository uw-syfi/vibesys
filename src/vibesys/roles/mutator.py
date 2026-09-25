"""Mutator role family: evolve's LLM-as-mutation-operator.

Evolve reuses the ``implementer`` agent kind for the mutation operator (same
backend/model config lookup as every other strategy's implementer), so
``CANDIDATE_MUTATOR.id == "implementer"``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from vibesys.runtime import Reuse, Role, Writes


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
    access=Writes(),
    session=Reuse(),
    message=(
        "Edit the workspace to produce an offspring of the parent. "
        "Then return one JSON object matching the schema above."
    ),
)

ALL_ROLES = (CANDIDATE_MUTATOR,)
