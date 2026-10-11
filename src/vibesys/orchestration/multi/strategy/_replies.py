"""The parts of an agent reply the strategy reads, decoded at the boundary.

The agent-visible schema of the profiler's reply is `ProfilerSummary` (the profiler guide's
contract, also used by `evolve`); the strategy keeps only the findings the designer is shown, so
it decodes the same JSON into this smaller model and ignores the rest.
"""

from pydantic import BaseModel, ConfigDict, FiniteFloat


class ProfilerFindings(BaseModel):
    """What the profiler found, as the planning request shows it."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    analysis: str
    bottlenecks: str
    suggestions: str
    perf_metric: FiniteFloat | None = None
    perf_unit: str | None = None
