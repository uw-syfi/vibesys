"""Policy-owned values for the single-agent orchestration presets."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibesys.evaluators import input_manifest
from vibesys.orchestrations.agent_options import AgentOrchestrationOptions
from vibesys.roles.single_agent import SingleAgentRoundResponse
from vibesys.search.hypothesis.state import HypothesisState
from vs_runtime.api import AccuracyReceipt

_ProfileGuidedInput = input_manifest.ProfileGuidedInput


class SingleOptions(AgentOrchestrationOptions):
    """Strict production descriptor options for the profiling-off preset."""

    profile_guided: Literal[None] = None

    @model_validator(mode="after")
    def _registered_values(self) -> SingleOptions:
        if self.interface not in {"inprocess", "service"}:
            message = f"unsupported single-agent interface {self.interface!r}"
            raise ValueError(message)
        if self.memory_layout not in {"files", "directories"}:
            message = f"unsupported single-agent memory_layout {self.memory_layout!r}"
            raise ValueError(message)
        return self


class ProfileGuidedSingleOptions(AgentOrchestrationOptions):
    """Strict production descriptor options for the profiling-on preset."""

    profile_guided: _ProfileGuidedInput

    @model_validator(mode="after")
    def _registered_values(self) -> ProfileGuidedSingleOptions:
        if self.interface not in {"inprocess", "service"}:
            message = f"unsupported profile-guided single-agent interface {self.interface!r}"
            raise ValueError(message)
        if self.memory_layout not in {"files", "directories"}:
            message = (
                f"unsupported profile-guided single-agent memory_layout {self.memory_layout!r}"
            )
            raise ValueError(message)
        return self


class PaidAttempt(BaseModel):
    """Durable proof that one implementer attempt was started."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round_number: Annotated[int, Field(gt=0)]
    role_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)
    turn_number: Annotated[int, Field(gt=0)]


class SingleState(BaseModel):
    """The single plugin's complete opaque durability aggregate."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = 1
    search: HypothesisState = Field(default_factory=HypothesisState)
    last_paid_attempt: PaidAttempt | None = None
    accuracy_receipt: AccuracyReceipt | None = None
    last_response: SingleAgentRoundResponse | None = None


__all__ = ["PaidAttempt", "ProfileGuidedSingleOptions", "SingleOptions", "SingleState"]
