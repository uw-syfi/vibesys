"""Public surface for the dynamic orchestration plugin."""

from typing import TYPE_CHECKING

from vibesys.orchestration.dynamic.models import (
    DynamicOptions,
    EvidenceReference,
    ImplementerResult,
    ImplementPortfolioPlan,
    PortfolioPlan,
    ProfileDecision,
    ProfilePlan,
    ReviewResult,
    WorkstreamPlan,
)

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.plugin import PLUGIN, REGISTRATION


def __getattr__(name: str) -> object:
    """Load execution wiring only when requested, keeping value and shell imports acyclic."""
    if name in {"PLUGIN", "REGISTRATION"}:
        # Eager wiring imports create a cycle through the execution shell;
        # importlib would hide the declared dependency behind runtime loading.
        from vibesys.orchestration.dynamic.plugin import (  # noqa: PLC0415  # lint-waiver: LW-641001 [PLC0415]; eager wiring creates a shell cycle; importlib hides the declared dependency.
            PLUGIN,
            REGISTRATION,
        )

        return PLUGIN if name == "PLUGIN" else REGISTRATION
    raise AttributeError(name)


__all__ = [
    "PLUGIN",
    "REGISTRATION",
    "DynamicOptions",
    "EvidenceReference",
    "ImplementPortfolioPlan",
    "ImplementerResult",
    "PortfolioPlan",
    "ProfileDecision",
    "ProfilePlan",
    "ReviewResult",
    "WorkstreamPlan",
]
