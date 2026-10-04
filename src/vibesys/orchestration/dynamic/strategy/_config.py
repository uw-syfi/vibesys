"""Static policy knobs of one dynamic run, built from the validated `DynamicOptions`.

Configuration is an input of the strategy, never persisted state: restarting the
strategy with the same options reproduces every scientific decision.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.metrics import MetricSpace

if TYPE_CHECKING:
    from vibesys.orchestration.dynamic.models import DynamicOptions

type Positive = Annotated[int, Field(gt=0)]
type Duration = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class DynamicConfig(BaseModel):
    """Scientific policy: budgets, cadence, ranking space and per-role deadlines."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_rounds: Positive
    max_in_flight: Positive = 2
    judge_every: Positive = 1
    max_retries_per_round: Positive = 1
    # Planner correction bound: the first reply plus this many corrections.
    max_corrections: Annotated[int, Field(ge=0)] = 1
    max_input_measurement_attempts: Positive = 3
    metric_space: MetricSpace = Field(default_factory=MetricSpace)
    benchmark_configured: bool = True
    accuracy_configured: bool = True
    profiling: bool = False
    planner_turn_seconds: Duration = 1800.0
    implementer_turn_seconds: Duration = 7200.0
    judge_turn_seconds: Duration = 1800.0
    profiler_turn_seconds: Duration = 3600.0
    queue_allowance_seconds: Duration = 900.0
    accuracy_seconds: Duration = 1800.0
    benchmark_seconds: Duration = 3600.0
    profile_seconds: Duration = 1800.0
    operation_seconds: Duration = 300.0

    @property
    def start_budget(self) -> int:
        """Workstreams the run may schedule: every one is one round of agent work."""
        return self.max_rounds * self.max_in_flight

    @classmethod
    def from_options(cls, options: DynamicOptions, **overrides: object) -> DynamicConfig:
        """Project the validated plugin options; `overrides` supply launch capabilities."""
        return cls.model_validate(
            {
                "max_rounds": options.max_rounds,
                "max_in_flight": options.max_in_flight,
                "judge_every": options.judge_every,
                "max_retries_per_round": options.max_retries_per_round,
                "metric_space": options.metric_space,
                **overrides,
            }
        )
