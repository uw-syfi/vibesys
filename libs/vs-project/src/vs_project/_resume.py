"""Resume compatibility policy for persisted run configurations."""

from dataclasses import dataclass
from typing import Literal

from vs_project._state import (
    AgentRunConfiguration,
    EvolveRunConfiguration,
    RunConfiguration,
)

ResumeLimitField = Literal["max_rounds", "max_generations"]


@dataclass(frozen=True)
class ResumeConfigurationComparison:
    """Typed result of comparing recorded and requested resume settings."""

    changed_fields: tuple[str, ...]
    limit_field: ResumeLimitField
    recorded_limit: int
    requested_limit: int
    migration_required: bool


def compare_resume_configurations(
    recorded: RunConfiguration,
    requested: RunConfiguration,
) -> ResumeConfigurationComparison:
    """Compare same-loop configurations under the persisted resume policy."""
    if recorded.outer_loop != requested.outer_loop:
        raise ValueError(  # noqa: TRY003  # tracked: #288
            "resume configuration outer loops must match"
        )

    limit_field, recorded_limit = _resume_limit(recorded)
    _, requested_limit = _resume_limit(requested)

    migration_required = _requires_legacy_agent_objectives_migration(recorded, requested)
    changed_fields = tuple(
        sorted(
            field
            for field in type(recorded).model_fields
            if field != limit_field
            and not (migration_required and field == "objectives")
            and getattr(recorded, field) != getattr(requested, field)
        )
    )
    return ResumeConfigurationComparison(
        changed_fields=changed_fields,
        limit_field=limit_field,
        recorded_limit=recorded_limit,
        requested_limit=requested_limit,
        migration_required=migration_required,
    )


def _resume_limit(configuration: RunConfiguration) -> tuple[ResumeLimitField, int]:
    if isinstance(configuration, EvolveRunConfiguration):
        return "max_generations", configuration.max_generations
    return "max_rounds", configuration.max_rounds


def _requires_legacy_agent_objectives_migration(
    recorded: RunConfiguration,
    requested: RunConfiguration,
) -> bool:
    if not isinstance(recorded, AgentRunConfiguration) or not isinstance(
        requested, AgentRunConfiguration
    ):
        return False
    return (
        recorded.outer_loop == requested.outer_loop == "agent"
        and "objectives" not in recorded.model_fields_set
        and bool(requested.objectives)
    )
