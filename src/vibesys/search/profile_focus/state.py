"""Persisted state types for the profile-focus search."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, model_validator


class ProfileGuidanceStatus(StrEnum):
    """Framework-owned lifecycle state for one profiled component."""

    OPEN = "open"
    ACTIVE = "active"
    EXHAUSTED = "exhausted"


class ProfileBottleneck(BaseModel):
    """One ranked cost center from a task-owned profiler."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: str = Field(min_length=1)
    cost: Annotated[FiniteFloat, Field(ge=0)]
    share: Annotated[FiniteFloat, Field(ge=0, le=1)]
    evidence: list[str] = Field(default_factory=list)


class ProfileAttributionSample(BaseModel):
    """A component's measured profile share in one planning round."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: Annotated[int, Field(gt=0)]
    cost: Annotated[FiniteFloat, Field(ge=0)]
    share: Annotated[FiniteFloat, Field(ge=0, le=1)]
    evidence: list[str] = Field(default_factory=list)


class ProfileImprovementSample(BaseModel):
    """A direction-normalized relative improvement in one completed round."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: Annotated[int, Field(gt=0)]
    relative_improvement: FiniteFloat


class ProfileGuidedComponent(BaseModel):
    """Typed durable state for one component in a profile-guided walk."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(min_length=1)
    status: ProfileGuidanceStatus = ProfileGuidanceStatus.OPEN
    rounds_spent: Annotated[int, Field(ge=0)] = 0
    stalled_rounds: Annotated[int, Field(ge=0)] = 0
    latest_cost: Annotated[FiniteFloat, Field(ge=0)] | None = None
    latest_share: Annotated[FiniteFloat, Field(ge=0, le=1)] | None = None
    attribution_history: list[ProfileAttributionSample] = Field(default_factory=list)
    improvement_history: list[ProfileImprovementSample] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ordered_history(self) -> Self:
        for name, samples in (
            ("attribution", self.attribution_history),
            ("improvement", self.improvement_history),
        ):
            rounds = [sample.round for sample in samples]
            if rounds != sorted(set(rounds)):
                raise ValueError(  # noqa: TRY003
                    f"{name} history rounds must be unique and ordered"
                )
        return self


class ProfileFocusState(BaseModel):
    """Authoritative run-scoped cursor for profile-guided hypotheses."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    active_component: str | None = None
    components: list[ProfileGuidedComponent] = Field(default_factory=list)
    # Which round's attribution ``ranked_bottlenecks`` should render, mirroring
    # the ported controller's ephemeral, non-persisted ranking field: set by
    # the most recent ``observe()`` call, cleared once ``record()`` applies a
    # measured round to the active component. None outside that window, so a
    # component observed in an earlier round but not this one does not
    # resurface in the ranking.
    ranking_round: Annotated[int, Field(gt=0)] | None = None

    @model_validator(mode="after")
    def _valid_cursor(self) -> Self:
        names = [component.name for component in self.components]
        if len(names) != len(set(names)):
            raise ValueError("profile-guided component names must be unique")  # noqa: TRY003
        active = [
            component.name
            for component in self.components
            if component.status is ProfileGuidanceStatus.ACTIVE
        ]
        if active != ([self.active_component] if self.active_component is not None else []):
            raise ValueError(  # noqa: TRY003
                "active_component must name the only active component"
            )
        return self


__all__ = [
    "ProfileAttributionSample",
    "ProfileBottleneck",
    "ProfileFocusState",
    "ProfileGuidanceStatus",
    "ProfileGuidedComponent",
    "ProfileImprovementSample",
]
