"""Typed data the strategy hands to the declared prompt-rendering operation.

The strategy never builds prompt text. It sends a `PromptContext`; the operation's executor
renders the named template with `vs_prompts` and stores the artifacts. A child's workspace is an
isolated child of its parent's revision, so what the legacy loop kept in the population file
(the parent, the inspirations and the lessons of failed cold starts) is rendered from the typed
rows below.
"""

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field

from vibesys.orchestration.evolve.population import Individual
from vibesys.prompts import ReplyCorrectionPrompt, ResumePrompt, WaitUnrecordedPrompt
from vs_core.api import EvidenceRef, RevisionRef, Value


class PromptTemplate(StrEnum):
    """Closed set of evolve's own templates (the shared protocol ones are `CoreProtocolTemplate`)."""

    MUTATE = "mutate"
    JUDGE = "judge"
    PROFILER = "profiler"


class ObjectiveAxis(Value):
    """One directed objective axis the profiler must report."""

    name: str = Field(min_length=1)
    direction: Literal["max", "min"]


class MutatePrompt(Value):
    """One mutation request: a cold start, a repair of an earlier cold start, or a child."""

    template: Literal[PromptTemplate.MUTATE] = PromptTemplate.MUTATE
    base_revision: RevisionRef
    is_cold_start: bool
    # The workspace already holds the previous cold start's files, to be repaired in place.
    repair_seed: bool = False
    # Distinct feedback of the most recent failed individuals, newest first.
    failed_lessons: tuple[str, ...] = ()
    num_failed_attempts: int = Field(default=0, ge=0)
    parent: Individual | None = None
    inspirations: tuple[Individual, ...] = ()
    reference_path: str = "."
    interface: str = "inprocess"


class JudgePrompt(Value):
    """The independent review of one offspring."""

    template: Literal[PromptTemplate.JUDGE] = PromptTemplate.JUDGE
    candidate: RevisionRef
    interface: str = "inprocess"
    # The trusted evaluations the offspring was measured by, stated at render time.
    cited_evidence: tuple[EvidenceRef, ...] = ()


class ProfilerPrompt(Value):
    """One advisory profiling turn of a passing offspring."""

    template: Literal[PromptTemplate.PROFILER] = PromptTemplate.PROFILER
    candidate: RevisionRef
    profiler_kind: str
    profile_execution: str
    benchmark_command: str | None = None
    objectives: tuple[ObjectiveAxis, ...] = ()
    profiler_support_name: str
    profiler_mcp_name: str


type PromptContext = Annotated[
    MutatePrompt
    | JudgePrompt
    | ProfilerPrompt
    | ResumePrompt
    | ReplyCorrectionPrompt
    | WaitUnrecordedPrompt,
    Field(discriminator="template"),
]

__all__ = [
    "JudgePrompt",
    "MutatePrompt",
    "ObjectiveAxis",
    "ProfilerPrompt",
    "PromptContext",
    "PromptTemplate",
    "ReplyCorrectionPrompt",
    "ResumePrompt",
    "WaitUnrecordedPrompt",
]
