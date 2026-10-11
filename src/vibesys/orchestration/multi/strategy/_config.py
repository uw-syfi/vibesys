"""Static policy knobs of one multi run, built from the validated options.

Configuration is an input of the strategy, never persisted state: restarting the strategy with
the same options reproduces every decision.
"""

from typing import Annotated

from pydantic import Field

from vibesys.candidates.api import CandidateConfig
from vibesys.hypothesis import HypothesisConfig
from vibesys.serial_search.api import AttributionConfig
from vs_core.api import ArtifactRef, WorkspaceMode

type Positive = Annotated[int, Field(gt=0)]
type Duration = Annotated[float, Field(gt=0, allow_inf_nan=False)]


class MultiConfig(CandidateConfig):
    """Scientific policy: budgets, cadence, ranking space and per-role deadlines."""

    # The evaluation recipe artifact every measurement plan cites.
    recipe: ArtifactRef
    max_rounds: Positive
    # Paid implementer turns of one round; also the attempt's paid-invocation limit.
    max_retries_per_round: Positive = 1
    official_eval_every: Positive = 1
    # Review every N rounds; nominated or fresh claims and the final round are always reviewed.
    judge_every: Positive = 1
    max_continuation_rounds: Annotated[int, Field(ge=0)] = 2
    # Reply correction bound per logical turn: the first reply plus this many corrections.
    max_corrections: Annotated[int, Field(ge=0)] = 1
    # Turns the provider connection drops before a reply: each is asked again, up to this
    # many times per logical turn, without spending a correction or a paid retry.
    max_turn_drops: Annotated[int, Field(ge=0)] = 2
    turn_drop_backoff_seconds: Duration = 5.0
    turn_drop_backoff_cap_seconds: Duration = 120.0
    workspace_mode: WorkspaceMode = WorkspaceMode.ISOLATED_CHILD
    attribution: AttributionConfig | None = None
    # The run's selected profiler (`none` offers no profiling and skips the routing turn).
    profiler_id: str = "none"
    # The profiler's packaged analysis directory and the name of its tool server.
    profiler_support_name: str = ""
    profiler_mcp_name: str = ""
    # How the profiler is reached: `local` or `remote` capture, named in its prompt.
    profile_execution: str = "local"
    benchmark_command: str | None = None
    modality: str | None = None
    # Where the input's reference material is, as the run states it (`.` when there is none).
    reference_path: str = "."
    interface: str = "inprocess"
    orchestrator_turn_seconds: Duration = 1800.0
    profiler_turn_seconds: Duration = 3600.0
    implementer_turn_seconds: Duration = 7200.0
    judge_turn_seconds: Duration = 3600.0
    queue_allowance_seconds: Duration = 900.0
    accuracy_seconds: Duration = 1800.0
    benchmark_seconds: Duration = 3600.0
    operation_seconds: Duration = 300.0

    def drop_backoff(self, drops: int) -> float:
        """Seconds to wait before re-asking a turn that was already lost ``drops`` times."""
        return min(self.turn_drop_backoff_cap_seconds, self.turn_drop_backoff_seconds * 2**drops)

    @property
    def profiling(self) -> bool:
        """Whether the run offers a profiler, so the routing and profiling turns can happen."""
        return self.profiler_id != "none"

    @property
    def hypotheses(self) -> HypothesisConfig:
        """The hypothesis ledger's cadence and budget view of this configuration."""
        return HypothesisConfig(
            max_rounds=self.max_rounds,
            judge_every=self.judge_every,
            official_eval_every=self.official_eval_every,
            max_retries_per_round=self.max_retries_per_round,
            max_continuation_rounds=self.max_continuation_rounds,
        )
