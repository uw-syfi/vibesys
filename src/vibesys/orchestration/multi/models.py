"""Policy-owned values for the plain multi-agent orchestration."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from vibesys.evaluators import input_manifest
from vibesys.orchestration.agent_options import AgentOrchestrationOptions
from vibesys.search.hypothesis.state import HypothesisState
from vs_runtime.api import AccuracyReceipt

_ProfileGuidedInput = input_manifest.ProfileGuidedInput


class MultiOptions(AgentOrchestrationOptions):
    """Strict production descriptor options for the profiling-off preset."""

    profile_guided: Literal[None] = None

    @model_validator(mode="after")
    def _registered_values(self) -> MultiOptions:
        if self.interface not in {"inprocess", "service"}:
            message = f"unsupported multi-agent interface {self.interface!r}"
            raise ValueError(message)
        if self.memory_layout not in {"files", "directories"}:
            message = f"unsupported multi-agent memory_layout {self.memory_layout!r}"
            raise ValueError(message)
        return self


class ProfileGuidedMultiOptions(AgentOrchestrationOptions):
    """Strict production descriptor options for the profiling-on preset."""

    profile_guided: _ProfileGuidedInput

    @model_validator(mode="after")
    def _registered_values(self) -> ProfileGuidedMultiOptions:
        if self.interface not in {"inprocess", "service"}:
            message = f"unsupported profile-guided multi-agent interface {self.interface!r}"
            raise ValueError(message)
        if self.memory_layout not in {"files", "directories"}:
            message = f"unsupported profile-guided multi-agent memory_layout {self.memory_layout!r}"
            raise ValueError(message)
        return self


class PaidAttempt(BaseModel):
    """Durable proof that one implementer attempt was started."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round_number: Annotated[int, Field(gt=0)]
    member_id: str = Field(min_length=1)
    turn_number: Annotated[int, Field(gt=0)]


class MultiState(BaseModel):
    """The multi plugin's complete opaque durability aggregate."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    schema_version: Literal[1] = 1
    search: HypothesisState = Field(default_factory=HypothesisState)
    last_paid_attempt: PaidAttempt | None = None
    accuracy_receipt: AccuracyReceipt | None = None


__all__ = ["MultiOptions", "MultiState", "PaidAttempt", "ProfileGuidedMultiOptions"]
