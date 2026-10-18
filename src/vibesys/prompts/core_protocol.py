"""The core lifecycle's agent-facing protocol: typed contexts and the templates they render.

Every strategy on the core lifecycle speaks the same protocol to its agents when a reply violates
its schema, when a suspended turn resumes after its awaited evaluations, when a reply waits and
the run recorded no wait, and when it explains the in-turn evaluation tool and the run
environment. The wording lives in ``core_protocol/*.j2``; a strategy lists the directory
(`vibesys.prompts.CORE_PROTOCOL_DIR`) as a fallback root of its core policy and adds the contexts below to its
own prompt-context union.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal

from pydantic import Field

from vs_core.api import MIN_SAMPLES_WITHOUT_FLOOR, VERDICT_NOISES, Value

if TYPE_CHECKING:
    from vs_core.api import ComparisonPolicy


class CoreProtocolTemplate(StrEnum):
    """The shared templates a strategy can request through its render operation."""

    RESUME = "resume"
    REPLY_CORRECTION = "reply_correction"
    WAIT_UNRECORDED = "wait_unrecorded"


class WaitUnrecordedPrompt(Value):
    """The previous reply said it was waiting for an evaluation, but the run recorded no wait."""

    template: Literal[CoreProtocolTemplate.WAIT_UNRECORDED] = CoreProtocolTemplate.WAIT_UNRECORDED


class ComparisonRule(Value):
    """How core reaches a comparison's verdict, as the core protocol templates state it.

    ``resume.j2`` renders it, so the agent is told the rule core applies
    (`vs_core._comparison`) for the run's task, not a fixed text that drifts from it.
    """

    noise_declared: bool
    """Whether the task declares its per-run noise (``relative_noise`` above 0)."""
    verdict_noises: float = Field(gt=0)
    """Under a declared noise, how many noises a difference must exceed for a verdict."""
    min_runs: int = Field(ge=1)
    """Without a declared noise, the runs each side needs before any verdict."""


def core_protocol_variables(policy: ComparisonPolicy) -> dict[str, object]:
    """The run-level variables the core protocol templates read, from the run's comparison policy.

    Every strategy on the core lifecycle adds them to its own run-level variables, from
    ``vs_core.api.policy_of(facts)``, as it adds the core protocol directory to its template
    roots.
    """
    return {
        "comparison_rule": ComparisonRule(
            noise_declared=policy.floor_declared,
            verdict_noises=VERDICT_NOISES,
            min_runs=MIN_SAMPLES_WITHOUT_FLOOR,
        )
    }
