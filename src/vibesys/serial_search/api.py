"""Public surface of the policy shared by the serial hypothesis-search strategies."""

from pathlib import Path

from vibesys.serial_search import prompts as _prompts
from vibesys.serial_search._attribution import (
    OUTPUT_ARGUMENT,
    AttributionConfig,
    bottlenecks_of,
    focus_of,
    focus_policy,
    observe,
    record_round,
)
from vibesys.serial_search._plan_view import (
    Bottleneck,
    FrontierRow,
    LastProfile,
    PendingClaim,
    PlanEvidence,
    RoundRow,
    plan_evidence,
)
from vibesys.serial_search._plans import accept_plan, base_commit

PROMPTS_DIR = Path(_prompts.__file__).parent
"""Where the shared `plan.j2` lives; a strategy lists it as a fallback root of its core policy."""

__all__ = [
    "OUTPUT_ARGUMENT",
    "PROMPTS_DIR",
    "AttributionConfig",
    "Bottleneck",
    "FrontierRow",
    "LastProfile",
    "PendingClaim",
    "PlanEvidence",
    "RoundRow",
    "accept_plan",
    "base_commit",
    "bottlenecks_of",
    "focus_of",
    "focus_policy",
    "observe",
    "plan_evidence",
    "record_round",
]
