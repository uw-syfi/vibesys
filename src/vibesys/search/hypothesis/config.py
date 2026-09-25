"""Static per-run configuration for the hypothesis search."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HypothesisConfig:
    """Cadence and budget knobs shared by every hypothesis-driven strategy.

    ``judge_every`` and the review-cadence overrides only matter for a
    designer/implementer split (multi, profile_multi); a single combined
    agent reviews its own work every round, which the default
    ``judge_every=1`` reproduces without a separate code path.
    """

    max_rounds: int
    judge_every: int = 1
    official_eval_every: int = 1
    max_retries_per_round: int = 1
    max_continuation_rounds: int = 2

    def __post_init__(self) -> None:
        """Reject configuration that cannot express a valid schedule."""
        if self.max_rounds < 1:
            message = "max_rounds must be positive"
            raise ValueError(message)
        if self.judge_every < 1:
            message = "judge_every must be positive"
            raise ValueError(message)
        if self.official_eval_every < 1:
            message = "official_eval_every must be positive"
            raise ValueError(message)
        if self.max_retries_per_round < 1:
            message = "max_retries_per_round must be positive"
            raise ValueError(message)
        if self.max_continuation_rounds < 0:
            message = "max_continuation_rounds must be non-negative"
            raise ValueError(message)
