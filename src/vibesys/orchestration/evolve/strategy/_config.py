"""Static policy knobs of one evolve run, built from the validated options.

Configuration is an input of the strategy, never persisted state: restarting the strategy with
the same options reproduces every decision.
"""

from typing import Annotated, Literal

from pydantic import Field

from vibesys.candidates.api import CandidateConfig
from vibesys.orchestration.evolve.population import (
    OpenEvolveSelectorConfig,
    PopulationConfig,
)
from vs_core.api import ArtifactRef, WorkspaceMode

type Positive = Annotated[int, Field(gt=0)]
type Duration = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class EvolveConfig(CandidateConfig):
    """Search policy: budgets, selection knobs, ranking space and per-role deadlines."""

    # The evaluation recipe artifact every measurement plan cites.
    recipe: ArtifactRef
    max_generations: Positive
    children_per_generation: Positive
    k_top_inspirations: Annotated[int, Field(ge=0)]
    k_random_inspirations: Annotated[int, Field(ge=0)]
    selection_temperature: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    seed: int | None = None
    selector: Literal["vibesys", "openevolve"] = "vibesys"
    openevolve: OpenEvolveSelectorConfig | None = None
    frontier_bias: Annotated[float, Field(ge=0, le=1)]
    # Cold-start attempts, each one a settled child; the first passing one seeds the population.
    bootstrap_max_attempts: Positive
    # Children in flight at once. Core admits no more than this (`Limits.max_parallel`), and the
    # strategy starts a slot only when fewer are in flight, so no start waits in core's queue.
    max_parallelism: Positive = 1
    # Reply correction bound per logical turn: the first reply plus this many corrections.
    max_corrections: Annotated[int, Field(ge=0)] = 1
    # Turns the provider connection drops before a reply: each is asked again, up to this
    # many times per logical turn, without spending a correction.
    max_turn_drops: Annotated[int, Field(ge=0)] = 2
    turn_drop_backoff_seconds: Duration = 5.0
    turn_drop_backoff_cap_seconds: Duration = 120.0
    workspace_mode: WorkspaceMode = WorkspaceMode.ISOLATED_CHILD
    # The run's selected profiler (`none` offers no profiling and skips the profiler turn).
    profiler_id: str = "none"
    profiler_support_name: str = ""
    profiler_mcp_name: str = ""
    profile_execution: str = "local"
    benchmark_command: str | None = None
    modality: str | None = None
    reference_path: str = "."
    interface: str = "inprocess"
    mutator_turn_seconds: Duration = 7200.0
    judge_turn_seconds: Duration = 3600.0
    profiler_turn_seconds: Duration = 3600.0
    queue_allowance_seconds: Duration = 900.0
    accuracy_seconds: Duration = 1800.0
    benchmark_seconds: Duration = 3600.0
    operation_seconds: Duration = 300.0

    def drop_backoff(self, drops: int) -> float:
        """Seconds to wait before re-asking a turn that was already lost ``drops`` times."""
        return min(self.turn_drop_backoff_cap_seconds, self.turn_drop_backoff_seconds * 2**drops)

    @property
    def profiling(self) -> bool:
        """Whether the run offers a profiler, so a passing child gets a profiler turn."""
        return self.profiler_id != "none"

    @property
    def needs_code(self) -> bool:
        """Whether admission needs each passing child's patch (the OpenEvolve selector)."""
        return self.selector == "openevolve"

    @property
    def population(self) -> PopulationConfig:
        """The population policy's view of this configuration."""
        return PopulationConfig(
            space=self.metric_space,
            selection_temperature=self.selection_temperature,
            frontier_bias=self.frontier_bias,
            k_top_inspirations=self.k_top_inspirations,
            k_random_inspirations=self.k_random_inspirations,
            selector=self.selector,
            openevolve=self.openevolve,
            seed=self.seed,
        )

    @property
    def total_attempts(self) -> int:
        """The most attempts a run can start: every cold start and every child."""
        return self.bootstrap_max_attempts + self.max_generations * self.children_per_generation
