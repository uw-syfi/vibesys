"""Typed data the single search hands to the prompt renderer.

The search never builds prompt text. It sends a `PromptContext`; the render operation renders
the role's template and stores the artifacts. The designer's request is shared with the multi
search. What the legacy loop kept in progress files is rendered from typed rows, because a
round's workspace is an isolated child of a revision and holds no progress files. The contexts
of the exchanges core starts (a reply that broke its schema, a resumed turn, a wait nobody
recorded) are core's, rendered by the role's `EXCHANGES` templates.
"""

from enum import StrEnum
from typing import Annotated, ClassVar, Literal

from pydantic import Field

from vibesys.orchestration.single.search._roles import IMPLEMENTER
from vibesys.serial_search.api import PlanCorrection, PlanPrompt
from vs_core.api.search import RevisionRef, RolePromptContext, SchemaRef


class PromptTemplate(StrEnum):
    """Closed set of single's own template: the designer's are `PlanTemplate`."""

    ROUND = "round"


class RoundPrompt(RolePromptContext):
    """One implementation, self-review and profile request for the open round."""

    reply_schema: ClassVar[SchemaRef | None] = IMPLEMENTER.reply_schema

    template: Literal[PromptTemplate.ROUND] = PromptTemplate.ROUND
    round_number: int = Field(gt=0)
    retry: int = Field(ge=1)
    hypothesis_id: str
    title: str = ""
    hypothesis: str
    task: str
    pass_criteria: str
    invariants: str = ""
    activation_evidence: str = ""
    falsification_criteria: str = ""
    minimum_ratio: float = 1.0
    base_revision: RevisionRef
    feedback: str | None = None
    no_valid_reply: str | None = None
    dominated_by_rounds: tuple[int, ...] = ()


type PromptContext = Annotated[
    PlanPrompt | PlanCorrection | RoundPrompt, Field(discriminator="template")
]
"""The union the render operation carries."""
