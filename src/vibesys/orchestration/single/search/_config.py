"""The single search's own knobs: how long to search, how many ideas to measure, when to give up.

Everything else a run needs belongs to someone else. How candidates are measured and compared
is the task's (`ctx.task`); how an agent is asked is its role's (`_roles`); how a backend
retries its infrastructure is the backend's. A config is an input of the search, never persisted
state: running the search again with the same config and the same events reproduces every
decision.
"""

from typing import Annotated

from pydantic import Field

from vibesys.hypothesis import HypothesisConfig
from vibesys.serial_search.api import AttributionConfig
from vs_core.api.search import Value

type Positive = Annotated[int, Field(gt=0, le=1000)]
type NonNegative = Annotated[int, Field(ge=0, le=1000)]


class SingleConfig(Value):
    """Budget, cadence and give-up rules of one single run."""

    max_rounds: Positive
    """Rounds of agent work: each opens one candidate workspace."""
    max_retries_per_round: Positive = 1
    """Paid implementer turns of one round, which is also the workspace's paid-turn limit."""
    max_continuation_rounds: NonNegative = 2
    """Times one hypothesis may be continued after its first round."""
    exclusive_root: bool = False
    """Whether rounds work in the run's own root, for a host that cannot open isolated candidate
    sandboxes. Follows the run environment; the operator does not choose it."""
    attribution: AttributionConfig | None = None
    """The profile-guided preset's attribution command and plateau policy; None otherwise."""

    @property
    def hypotheses(self) -> HypothesisConfig:
        """The hypothesis ledger's cadence and budget view of this configuration.

        A single agent reviews its own work in every round, so the review cadence is fixed.
        """
        return HypothesisConfig(
            max_rounds=self.max_rounds,
            max_retries_per_round=self.max_retries_per_round,
            max_continuation_rounds=self.max_continuation_rounds,
        )
