"""The knobs candidate policy reads, which a strategy's own config extends."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from vibesys.metrics import MetricSpace

type Positive = Annotated[int, Field(gt=0)]


class CandidateConfig(BaseModel):
    """Which gates a run measures, how candidates rank and how often the input is retried."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Core retries the input measurement only after an infrastructure failure, within this bound.
    max_input_measurement_attempts: Positive = 3
    metric_space: MetricSpace = Field(default_factory=MetricSpace)
    benchmark_configured: bool = True
    accuracy_configured: bool = True
