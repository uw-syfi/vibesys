"""Validated, versioned options shared by the four built-in agent orchestrators."""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.evaluators.input_manifest import ProfileGuidedInput
from vibesys.evaluators.metrics import MetricSpace, Objective

if TYPE_CHECKING:
    from vibesys.evaluators.input_manifest import BenchmarkResult

PortableText = Annotated[str, Field(min_length=1, max_length=256)]


class AgentOrchestrationOptions(BaseModel):
    """Strict execution options common to the four agent policy classes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    interface: PortableText
    modality: PortableText | None = None
    max_rounds: Annotated[int, Field(gt=0)]
    max_retries_per_round: Annotated[int, Field(gt=0)]
    judge_every: Annotated[int, Field(gt=0)]
    official_eval_every: Annotated[int, Field(gt=0)]
    memory_layout: PortableText
    operator_constraints: tuple[str, ...] = ()
    metric_space: MetricSpace = Field(default_factory=MetricSpace)
    profile_guided: ProfileGuidedInput | None = None


def recorded_metric_space(
    metrics: MetricSpace, benchmark_result: BenchmarkResult | None
) -> MetricSpace:
    """Include the benchmark axis without losing task noise tolerance."""
    if benchmark_result is None or metrics.axis(benchmark_result.metric) is not None:
        return metrics
    return metrics.model_copy(
        update={
            "objectives": (*metrics.objectives, Objective(benchmark_result.metric, "max")),
        }
    )
