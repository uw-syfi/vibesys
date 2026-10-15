"""What a dynamic run may spend, stated once from its config and its roles.

The run schedules `max_rounds * max_in_flight` workstreams, and each is one admitted attempt.
Core enforces the numbers; this module is the one place that derives them from what the search
does, so a change to a role's corrections or to the planner's bounds changes the budget with it.
"""

from vibesys.candidates.api import RunBudget, turn_chain
from vibesys.orchestration.dynamic.search._config import DynamicConfig
from vibesys.orchestration.dynamic.search._planner import (
    LOST_TURNS,
    MAX_CORRECTIONS,
    MAX_FRESH_ASKS,
)
from vibesys.orchestration.dynamic.search._roles import (
    IMPLEMENTER,
    JUDGE,
    MERGER,
    OUTLINER,
    PROFILER,
)
from vs_core.api.search import MAX_SUBMISSIONS_PER_MEASUREMENT

ADOPTION_ROUNDS = 3
"""AdoptRevision rounds core may spend on the winner, the first included."""


def budget(config: DynamicConfig) -> RunBudget:
    """The attempts, turns, parallelism and refunds a dynamic run may use."""
    starts = config.start_budget
    planning = (1 + MAX_CORRECTIONS + MAX_FRESH_ASKS) * LOST_TURNS.max_tries
    chain = max(
        turn_chain(corrections=role.corrections, lost_turns=role.lost_turns)
        for role in (IMPLEMENTER, JUDGE, MERGER, PROFILER)
    )
    # An implementer asked until a candidate reaches measurement, then once per resumed
    # measurement submission, and the judge once for each review.
    per_workstream = chain * (config.max_unmeasured_turns + 1 + MAX_SUBMISSIONS_PER_MEASUREMENT)
    # A run that follows a plan declaring no steps asks once for its outline.
    outlining = (
        turn_chain(corrections=OUTLINER.corrections, lost_turns=OUTLINER.lost_turns)
        if config.follows_plan and config.plan is not None and not config.plan.steps
        else 0
    )
    return RunBudget(
        attempts=starts,
        turns=(starts + 1) * planning + starts * per_workstream + outlining,
        parallel=config.max_in_flight,
        refunds=starts,
        adoption_rounds=ADOPTION_ROUNDS,
    )
