"""What a multi run may spend, stated once from its config and its roles.

A round is one admitted attempt, run serially, plus, when the run offers a profiler, the
profiler's own unretained attempt on the round's base, so the run admits `max_rounds` attempts
(twice that with profiling). Core
enforces the numbers; this module is the one place that derives them from what the search does,
so a change to a role's corrections or to the number of paid turns changes the budget with it.
"""

from vibesys.candidates.api import RunBudget, turn_chain
from vibesys.orchestration.multi.search._config import MultiConfig
from vibesys.orchestration.multi.search._roles import IMPLEMENTER, JUDGE, PROFILER, ROUTER
from vibesys.serial_search.api import ORCHESTRATOR, PLAN_CORRECTIONS
from vs_core.api.search import MAX_SUBMISSIONS_PER_MEASUREMENT, Role

ADOPTION_ROUNDS = 3
"""AdoptRevision rounds core may spend on the winner, the first included."""


def _chain(role: Role) -> int:
    return turn_chain(corrections=role.corrections, lost_turns=role.lost_turns)


def budget(config: MultiConfig) -> RunBudget:
    """The attempts, turns, parallelism and refunds a multi run may use."""
    designing = _chain(ORCHESTRATOR) * (1 + PLAN_CORRECTIONS)
    advising = _chain(ROUTER) + _chain(PROFILER) if config.profiling else 0
    paid = config.max_retries_per_round
    # The designer is asked, and corrected when the ledger refuses its plan; each paid
    # implementer turn is a chain with one resume per measurement submission, and each one may
    # be followed by a review (a chain of the judge's, resumed the same way).
    implementing = (_chain(IMPLEMENTER) + _chain(JUDGE)) * (paid + MAX_SUBMISSIONS_PER_MEASUREMENT)
    per_round = designing + advising + implementing
    return RunBudget(
        attempts=config.max_rounds * (2 if config.profiling else 1),
        turns=config.max_rounds * per_round,
        parallel=1,
        refunds=config.max_rounds,
        adoption_rounds=ADOPTION_ROUNDS,
    )
