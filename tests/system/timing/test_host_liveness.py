"""A host records in core that it is alive, at least once per lease renewal, even mid-turn.

A restart credits open limits with the time no host was alive, measured from the dead host's
last recorded renewal or commit. So at every moment of a run, core must already hold a proof
of life no older than one renewal interval (a third of the lease), or a host that died then
would have its successor credit limits with time it was alive. Turns here outlast the lease
and the loop reads its controls less often than that, so only the renewals keep core current.
"""

from __future__ import annotations

from itertools import pairwise

import pytest
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


@pytest.mark.parametrize(
    "publications_per_renewal",
    [pytest.param(2.0, marks=pytest.mark.slow), pytest.param(1.0, marks=pytest.mark.slow), 0.5],
)
def test_a_slow_publication_delays_the_proof_of_life_by_at_most_its_own_length(
    publications_per_renewal: float,
) -> None:
    """Core's proof of life is at most one renewal interval plus one publication old.

    Nothing commits while the loop awaits a publication, so a host that dies then is credited
    up to one publication on top of one renewal interval; that is the real bound. Before,
    a renewal reached core only at the top of the next loop iteration, so the publications
    of one iteration added up (three of them: 100 s against a 67 s bound), and the heartbeat
    slept a fixed period out of phase with the loop's renewals, so up to two intervals
    passed between renewals.
    """
    publish_s = _RENEWAL / publications_per_renewal
    timed = run_timed(
        StaggeredStrategy(config=StaggeredConfig(workers=1)),
        Plan(
            duration=lambda _worker, _ordinal: 1.5 * LEASE,
            control_poll_s=4 * LEASE,
            publish_s=publish_s,
        ),
    )

    assert timed.failure is None
    commits = [*timed.alive, (timed.ended_at, timed.ended_at)]
    stale = max(later - proof for (_, proof), (later, _) in pairwise(commits))
    assert stale <= _RENEWAL + publish_s + _SLACK_S
