"""A host records in core that it is alive, at least once per lease renewal, even mid-turn.

A restart credits open limits with the time no host was alive, measured from the dead host's
last recorded renewal or commit. So at every moment of a run, core must already hold a proof
of life no older than one renewal interval (a third of the lease), or a host that died then
would have its successor credit limits with time it was alive. Turns here outlast the lease
and the loop reads its controls less often than that, so only the renewals keep core current.
"""

from __future__ import annotations

from itertools import pairwise

from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st
from tests.support.skeleton_world import LEASE
from tests.system.skeleton_timing import Plan, StaggeredConfig, StaggeredStrategy, run_timed

from vs_sim.api.testing import Depth, depth, examples

_NIGHTLY = depth() is Depth.NIGHTLY
_RENEWAL = LEASE / 3
# A commit lands a scheduling step after the renewal it records.
_SLACK_S = 1.0


@settings(
    max_examples=examples(pr=1, nightly=8),
    phases=(Phase.explicit, Phase.generate) if _NIGHTLY else (Phase.explicit,),
    deadline=None,
    derandomize=True,
    database=None,
)
@example(durations=[1.5 * LEASE])
@given(
    durations=st.lists(
        st.integers(min_value=20, max_value=300).map(float), min_size=1, max_size=2, unique=True
    )
)
def test_core_always_holds_a_proof_of_life_at_most_one_renewal_old(
    durations: list[float],
) -> None:
    timed = run_timed(
        StaggeredStrategy(config=StaggeredConfig(workers=len(durations))),
        Plan(
            duration=lambda worker, _ordinal: durations[worker],
            max_parallel=len(durations),
            control_poll_s=4 * LEASE,
        ),
    )

    assert timed.failure is None
    commits = [*timed.alive, (timed.ended_at, timed.ended_at)]
    stale = max(later - proof for (_, proof), (later, _) in pairwise(commits))
    assert stale <= _RENEWAL + _SLACK_S
