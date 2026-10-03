"""Shared policy for monotonic round-budget resume."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Protocol, cast

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vs_runtime.api import OrchestrationResumeDecision

if TYPE_CHECKING:
    from pydantic import BaseModel

    from vs_project.api import OrchestrationDescriptor


class _RoundBudgetOptions(Protocol):
    max_rounds: int


def compare_round_budget(
    recorded: OrchestrationDescriptor,
    requested: OrchestrationDescriptor,
    *,
    plugin_id: str,
    options_type: type[BaseModel],
) -> OrchestrationResumeDecision:
    """Allow only a monotonic ``max_rounds`` increase for one plugin."""
    if (
        recorded.id != plugin_id
        or requested.id != plugin_id
        or recorded.config_version != 1
        or requested.config_version != 1
    ):
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message="resuming a run cannot change its orchestration ID or config version",
            )
        )
    old = options_type.model_validate_json(json.dumps(recorded.options), strict=True)
    new = options_type.model_validate_json(json.dumps(requested.options), strict=True)
    changed = tuple(
        name
        for name in options_type.model_fields
        if name != "max_rounds" and getattr(old, name) != getattr(new, name)
    )
    if changed:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=(
                    "resuming a run cannot change its recorded configuration "
                    f"fields: {', '.join(changed)}"
                ),
            )
        )
    old_rounds = cast("_RoundBudgetOptions", old).max_rounds
    new_rounds = cast("_RoundBudgetOptions", new).max_rounds
    if new_rounds < old_rounds:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=(
                    "max_rounds is the run's total limit and cannot decrease when resuming "
                    f"(recorded {old_rounds}, requested {new_rounds})"
                ),
            )
        )
    if new_rounds == old_rounds:
        return OrchestrationResumeDecision(descriptor=None)
    return OrchestrationResumeDecision(descriptor=requested, requires_clean_workspace=True)


def project_round_budget(options: BaseModel, *, options_type: type[BaseModel]) -> int:
    """Project the validated ``max_rounds`` option for run presentation."""
    return cast("_RoundBudgetOptions", options_type.model_validate(options)).max_rounds


__all__ = ["compare_round_budget", "project_round_budget"]
