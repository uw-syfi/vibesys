"""A run that reaches its deadline keeps the verified candidates it already found.

A time budget (`[run] max_run_seconds`) is the normal way an optimization run ends, so
the host maps a deadline that adopted a retained, verified candidate to success
(`vibesys.run.core_run`). That needs the strategy to adopt its winner after the
deadline's drain stop. The scenario is the production shell and loop on virtual time
(`tests/support/scale_dynamic_run.py`): two workstreams per round, two rounds, with the
deadline in the middle of the second round, after the first round's candidates settled.
"""

from __future__ import annotations

from tests.support.scale_dynamic_run import Scenario, run_scale
from tests.support.timed_dynamic_run import TimingProfile

from vs_core.api import RetainedCandidate, RunStatus
from vs_slurm.api import SecondsRange, SlurmTimingProfile

_EXACT = TimingProfile(
    turn_s=SecondsRange.exactly(40.0),
    slurm=SlurmTimingProfile(
        queue_wait_s=SecondsRange.exactly(30.0),
        run_s=SecondsRange.exactly(150.0),
        completing_s=SecondsRange.exactly(35.0),
    ),
)
# Short role budgets keep every turn inside the run, so only the deadline's stop matters.
_BUDGETS = {
    "planner_turn_seconds": 5.0,
    "implementer_turn_seconds": 5.0,
    "judge_turn_seconds": 5.0,
    "operation_seconds": 5.0,
}


def test_a_deadline_after_verified_candidates_adopts_the_best_one() -> None:
    reference = run_scale(Scenario(in_flight=2, rounds=2, profile=_EXACT, config=_BUDGETS))
    first_round = [s for s in reference.turns if s.role == "judge"][:2]
    second_round = [s for s in reference.turns if s.role == "implementer"][2:]
    assert first_round
    assert second_round
    deadline = (second_round[0].start + second_round[0].end) / 2
    assert deadline > max(span.end for span in first_round)

    run = run_scale(
        Scenario(in_flight=2, rounds=2, profile=_EXACT, config=_BUDGETS, deadline_at=deadline)
    )

    assert run.error is None, run.error
    assert run.core.run.status is RunStatus.TERMINAL
    eligible = [item for item in run.core.settlement.settlements if item.eligible]
    assert eligible, "the first round settled verified candidates before the deadline"
    adoption = run.core.settlement.adoption
    assert adoption is not None, (
        run.core.run.result,
        [receipt.feedback for receipt in run.core.run.receipts[-1:]],
    )
    assert adoption.verified
    assert isinstance(adoption.selection, RetainedCandidate)
