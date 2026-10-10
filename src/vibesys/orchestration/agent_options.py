"""Validated, versioned options shared by the four built-in agent orchestrators."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.inputs import ProfileGuidedInput
from vibesys.metrics import MetricSpace

PortableText = Annotated[str, Field(min_length=1, max_length=256)]


class AgentOrchestrationOptions(BaseModel):
    """Strict execution options common to the four agent policy classes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    interface: PortableText
    modality: PortableText | None = None
    max_rounds: Annotated[int, Field(gt=0)]
    max_retries_per_round: Annotated[int, Field(gt=0)]
    judge_every: Annotated[int, Field(gt=0)]
    official_eval_every: Annotated[int, Field(gt=0)]
    operator_constraints: tuple[str, ...] = ()
    metric_space: MetricSpace = Field(default_factory=MetricSpace)
    profile_guided: ProfileGuidedInput | None = None
