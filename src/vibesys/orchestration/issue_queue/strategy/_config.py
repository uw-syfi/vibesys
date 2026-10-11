"""Static policy knobs of one issue-queue run, built from the validated options.

Configuration is an input of the strategy, never persisted state: restarting the strategy with
the same options reproduces every decision.
"""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from vs_core.api import ArtifactRef

type Positive = Annotated[int, Field(gt=0)]
type Duration = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class IssueQueueConfig(BaseModel):
    """Budgets, bounds and per-role deadlines of one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # The evaluation recipe artifact every measurement plan cites.
    recipe: ArtifactRef
    max_rounds: Positive
    # Paid implementer turns one issue may use before it is blocked.
    max_attempts_per_issue: Positive
    # The evaluator's cap on issues it files in one reply.
    max_issues_per_perf_eval: Positive
    # Issues worked in one round before the performance evaluation.
    max_issues_per_round: Positive
    # Reply correction bound: the first reply plus this many corrections.
    max_corrections: Annotated[int, Field(ge=0)] = 1
    # Turns the provider connection drops before a reply: each is asked again, up to this
    # many times per logical turn, without spending a correction or a paid turn.
    max_turn_drops: Annotated[int, Field(ge=0)] = 2
    turn_drop_backoff_seconds: Duration = 5.0
    turn_drop_backoff_cap_seconds: Duration = 120.0
    # Times one board request is sent again after its effect stayed unknown.
    max_board_retries: Annotated[int, Field(ge=0)] = 3
    implementer_turn_seconds: Duration = 7200.0
    judge_turn_seconds: Duration = 1800.0
    perf_turn_seconds: Duration = 7200.0
    operation_seconds: Duration = 300.0
    # The configured benchmark workloads as sorted-key JSON, or None to let the evaluator discover them.
    load_levels_json: str | None = None
    # Which gates the run measures. Adoption needs a trusted accuracy measurement of the final
    # candidate, so a run without an accuracy gate cannot adopt what its issues built.
    accuracy_configured: bool = True
    benchmark_configured: bool = True
    # Times core sends one measurement again after an infrastructure failure.
    max_measurement_submissions: Positive = 3
    queue_allowance_seconds: Duration = 900.0
    accuracy_seconds: Duration = 1800.0
    benchmark_seconds: Duration = 3600.0
    profile_seconds: Duration = 1800.0

    def drop_backoff(self, drops: int) -> float:
        """Seconds to wait before re-asking a turn that was already lost ``drops`` times."""
        return min(self.turn_drop_backoff_cap_seconds, self.turn_drop_backoff_seconds * 2**drops)

    @property
    def chain(self) -> int:
        """Most turns one logical reply can take: the first plus corrections and re-asks."""
        return 1 + self.max_corrections + self.max_turn_drops

    @property
    def max_attempts(self) -> int:
        """Attempts a run can start: each round works at most the cap of issues and one evaluation."""
        return self.max_rounds * (self.max_issues_per_round + 1)
