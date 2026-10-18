"""Strict configuration and durable state for evolutionary search."""

from __future__ import annotations

from typing import Annotated, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from vibesys.hypothesis import DecidedFrom
from vibesys.orchestration.evolve.population import OpenEvolveSelectorConfig
from vibesys.orchestration.review import Verdict
from vs_core.api.search import RevisionReply


class MutatorResponse(RevisionReply):
    """Mutation rationale recorded on the resulting population member.

    Every mutation claims work, so core checkpoints the turn's workspace and hands the checkpoint
    to the search with this reply.
    """

    summary: str = Field(
        description=(
            "Short description of the change made to the parent, or the initial "
            "candidate built or repaired during bootstrap."
        )
    )
    hypothesis: str = Field(
        description="Why this change is expected to improve the headline metric.",
    )
    expected_behavior: str = Field(description="Observable change a reviewer should expect.")


class JudgeResponse(BaseModel):
    """Review decision for one evolve candidate."""

    model_config = ConfigDict(extra="forbid")

    analysis: str = Field(description="Detailed analysis of the candidate implementation.")
    feedback: str = Field(
        description="Specific actionable feedback for the mutator. Empty string if passing."
    )
    verdict: Annotated[Verdict, DecidedFrom("cited_evidence")] = Field(
        description="PASS if all criteria are met, FAIL otherwise."
    )


class OpenEvolveOverrides(TypedDict, total=False):
    """CLI scalar overrides before evolve resolves persisted defaults."""

    openevolve_population_size: int | None
    openevolve_archive_size: int | None
    openevolve_num_islands: int | None
    openevolve_migration_interval: int | None
    openevolve_migration_rate: float | None


def resolve_openevolve_options(
    search_policy: str | None, values: OpenEvolveOverrides
) -> tuple[str | None, dict[str, int | float | None]]:
    """Resolve CLI overrides against the evolve-owned search defaults."""
    population_size = values.get("openevolve_population_size")
    archive_size = values.get("openevolve_archive_size")
    num_islands = values.get("openevolve_num_islands")
    migration_interval = values.get("openevolve_migration_interval")
    migration_rate = values.get("openevolve_migration_rate")
    if all(
        value is None
        for value in (
            population_size,
            archive_size,
            num_islands,
            migration_interval,
            migration_rate,
        )
    ):
        return search_policy, {
            "openevolve_population_size": None,
            "openevolve_archive_size": None,
            "openevolve_num_islands": None,
            "openevolve_migration_interval": None,
            "openevolve_migration_rate": None,
        }
    defaults = OpenEvolveSelectorConfig()
    config = OpenEvolveSelectorConfig(
        population_size=population_size or defaults.population_size,
        archive_size=archive_size or defaults.archive_size,
        num_islands=num_islands or defaults.num_islands,
        migration_interval=migration_interval or defaults.migration_interval,
        migration_rate=migration_rate if migration_rate is not None else defaults.migration_rate,
    )
    resolved = {
        "openevolve_population_size": config.population_size,
        "openevolve_archive_size": config.archive_size,
        "openevolve_num_islands": config.num_islands,
        "openevolve_migration_interval": config.migration_interval,
        "openevolve_migration_rate": config.migration_rate,
    }
    return search_policy or "openevolve", resolved


__all__ = [
    "JudgeResponse",
    "MutatorResponse",
    "resolve_openevolve_options",
]
