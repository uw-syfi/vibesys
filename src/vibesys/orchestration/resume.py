"""Neutral contracts for versioned orchestration persistence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from vs_project.api import OrchestrationDescriptor, RunEnvironmentRecord


@dataclass(frozen=True, slots=True)
class OrchestrationResumeDecision:
    """An owner-approved descriptor update and its workspace precondition."""

    descriptor: OrchestrationDescriptor | None
    requires_clean_workspace: bool = False


class ResumeConfigSnapshot(BaseModel):
    """Resolved common settings needed to reconstruct CLI configuration."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)

    model: str | None = None
    agent_backend: str
    agent_driver: str | None = None
    cli_provider: str | None = None
    cli_timeout: int | None = None
    compute_backend: str
    default_reasoning_effort: str | None = None
    outer_model: str | None = None
    outer_reasoning_effort: str | None = None
    inner_model: str | None = None
    inner_reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class ResumeProjection:
    """Owner-produced settings for restoring a version 4 run invocation."""

    orchestration_id: str
    run_environment: RunEnvironmentRecord
    config: ResumeConfigSnapshot
    cli_values: dict[str, object]
    budget_destination: str
    budget_value: int
