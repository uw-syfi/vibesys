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
    # Seconds to wait before asking an agent again after the provider connection dropped its
    # turn; doubles with each further drop of the same turn.
    turn_drop_backoff_seconds: Annotated[float, Field(gt=0, le=600)] = 5.0


def attribution_command_of(options: BaseModel) -> tuple[str, ...] | None:
    """The profile-guided attribution command a policy's options select, or ``None``."""
    if isinstance(options, AgentOrchestrationOptions) and options.profile_guided is not None:
        return options.profile_guided.command
    return None
