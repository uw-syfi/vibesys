"""Typed data the strategy hands to the declared prompt-rendering operation.

The strategy never builds prompt text. It sends a `PromptContext`; the operation's executor
renders the named template with `vs_prompts` and stores the artifacts. The evidence the legacy
loop kept in progress files (round history, the Pareto frontier, carried notices, the last
profile) is rendered from the typed rows below, because a round's workspace is an isolated child
of a revision and holds no progress files.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from vibesys.prompts import ReplyCorrectionPrompt, ResumePrompt, WaitUnrecordedPrompt
from vibesys.serial_search.api import PlanEvidence
from vs_core.api import RevisionRef, Value


class PromptTemplate(StrEnum):
    """Closed set of single's own templates (the shared protocol ones are `CoreProtocolTemplate`)."""

    PLAN = "plan"
    ROUND = "round"


class PlanPrompt(PlanEvidence):
    """One new-hypothesis planning request."""

    template: Literal[PromptTemplate.PLAN] = PromptTemplate.PLAN
    base_revision: RevisionRef


class RoundPrompt(Value):
    """One implementation, self-review and profile request for the open round."""

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
    minimum_acceptance_criteria: str = ""
    base_revision: RevisionRef
    feedback: str | None = None
    no_valid_reply: str | None = None
    dominated_by_rounds: tuple[int, ...] = ()
    official_due: bool = False
    official_reason: str | None = None
    interface: str = "inprocess"
    profiler_id: str = "none"
    profiler_support: str | None = None


type PromptContext = Annotated[
    PlanPrompt | RoundPrompt | ResumePrompt | ReplyCorrectionPrompt | WaitUnrecordedPrompt,
    Field(discriminator="template"),
]
