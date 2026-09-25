"""Mutator role family: evolve's LLM-as-mutation-operator reply schema.

Evolve has not yet been migrated onto ``ctx.agents.turn`` (see the phase
report), so this module holds only the reply schema today; its turn
sequencing still lives in ``loops/evolve/``. No ``Role`` value exists here
until that migration lands.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


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


ALL_ROLES: tuple = ()
