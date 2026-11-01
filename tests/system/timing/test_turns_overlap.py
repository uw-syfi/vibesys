"""Concurrent agent turns on a virtual clock: they overlap, each is followed up at its own end.

Run with every fake answering at once, a shell that runs one request at a time, one that waits
for the slowest turn before deciding, and one that lets a lease lapse during a long turn all
pass. Here each turn lasts a drawn number of virtual seconds (longer than the lease for some),
so the same properties are read off the timeline (``tests/system/skeleton_timing.py``).

Past bugs these guard, and the commits before which they fail:

- the shell ran one request at a time (#1374), and took its cap from a flag instead of the
  run's ``max_parallel`` (#1375): the overlap and critical-path checks;
- a finished turn's follow-up waited for every other running turn (#1375): the follow-up check;
- one dispatch longer than the lease lost the lease (#1370): the durations longer than the lease
  (``LEASE``) end the run only if the lease was renewed beside the turns.
"""

from __future__ import annotations

from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st
from tests.support.skeleton_strategy import IMPLEMENTER_ROLE
from tests.support.skeleton_world import LEASE
from tests.system.skeleton_timing import (
    Plan,
    StaggeredConfig,
    StaggeredStrategy,
    Timed,
    run_timed,
)

import vs_core.api as core
from vs_sim.api.testing import Depth, depth, examples

#: The loop wakes at most one observe interval after the last job ends (``Plan.observe_s``),
#: plus the cost of the cluster's own polls.
_SLACK_S = 6.0

#: How long core lets a turn run once the shell dispatches it, in seconds.
_TURN_TIME_S = IMPLEMENTER_ROLE.turn_time


# Whole seconds, all different: the scripted agent keeps one reply for all its turns, so two turns
# that end at the same instant would name the same commit.
_DURATIONS = st.lists(
    st.integers(min_value=20, max_value=250).map(float), min_size=2, max_size=3, unique=True
)

# A run costs seconds of host time, so a pull request replays the pinned examples only; the
# nightly depth also draws generated ones.
_NIGHTLY = depth() is Depth.NIGHTLY
_SETTINGS = settings(
    max_examples=examples(pr=1, nightly=8),
    phases=(Phase.explicit, Phase.generate) if _NIGHTLY else (Phase.explicit,),
    deadline=None,
    derandomize=True,
    database=None,
)


def _run(durations: list[float], cap: int) -> Timed:
    workers = len(durations)
    return run_timed(
        StaggeredStrategy(config=StaggeredConfig(workers=workers)),
        Plan(
            duration=lambda worker, _ordinal: durations[worker],
            max_parallel=cap,
            # The loop reads its controls less often than the lease lasts, so only the
            # heartbeat beside the turns keeps the lease.
            control_poll_s=4 * LEASE,
        ),
    )


@_SETTINGS
@example(durations=[30.0, 70.0])
@example(durations=[LEASE + 5, LEASE + 8, 30.0])
@given(durations=_DURATIONS)
def test_turns_run_at_once_and_each_is_followed_up_when_it_ends(durations: list[float]) -> None:
    """With a cap of the search's width, every turn is in flight at once, a finished turn is
    followed up (its change is submitted for evaluation) at the instant it ends while its peers
    still run, and the run lasts as long as its slowest turn, not the sum of them.
    """
    timed = _run(durations, cap=len(durations))

    assert timed.outcome is not None, "the run did not end by itself"
    assert timed.outcome.result is not None
    assert timed.outcome.result.outcome == "success"
    assert timed.timeline.peak_overlap() == len(durations)
    ends = sorted(span.end for span in timed.timeline.spans)
    assert len(ends) == len(durations)
    # Each turn's follow-up is submitted the moment the turn ends, not when the slowest ends.
    assert timed.cluster.submitted_at == ends
    assert timed.wall_s <= max(durations) + _SLACK_S, (timed.wall_s, durations)
    # A turn that outlasts the lease must not cost the run its lease.
    assert timed.holds_lease


@_SETTINGS
@example(durations=[30.0, 70.0, 50.0], width=2)
@example(durations=[247.0, 160.0], width=1)
@example(durations=[250.0, 249.0, 248.0], width=1)
@given(durations=_DURATIONS, width=st.integers(min_value=1, max_value=3))
def test_turns_in_flight_never_exceed_the_runs_parallelism(
    durations: list[float], width: int
) -> None:
    """The shell's cap is the run's ``max_parallel``: a smaller cap holds turns back, and the
    time a turn waits for a slot is not spent from its limit.

    Each turn's limit (``_TURN_TIME_S``) is measured from the instant the shell dispatches it, so
    a cap that serializes turns for longer than that limit ends no running turn: every turn lasts
    its own duration, and the run ends by itself.
    """
    cap = min(width, len(durations))
    timed = _run(durations, cap=cap)

    assert timed.outcome is not None
    assert timed.timeline.peak_overlap() == cap
    assert len(timed.timeline.spans) == len(durations)
    assert not any(span.cancelled for span in timed.timeline.spans)
    # Each turn's record ends its own limit after the instant the turn began, however long it
    # waited before that.
    limits = sorted(
        record.until.at
        for record in timed.core.records.records
        if isinstance(record.request, core.DispatchTurn)
    )
    assert limits == sorted(start + _TURN_TIME_S for start in timed.timeline.starts)


@_SETTINGS
@example(overrun=600.0)
@given(overrun=st.integers(min_value=600, max_value=900).map(float))
def test_a_turn_that_outlasts_its_limit_is_lost_at_the_limit_and_the_run_ends(
    overrun: float,
) -> None:
    """One turn lasts longer than its limit: it is cancelled at the limit, not waited for.

    Its dispatch ends unanswered, which once left the turn's session waiting for ever and halted
    the run. Core now treats it as a lost turn: it is cancelled, the ask ends, and the run ends
    well before the turn would have.
    """
    timed = run_timed(
        StaggeredStrategy(config=StaggeredConfig(workers=1)),
        Plan(duration=lambda _worker, _ordinal: overrun, max_parallel=1, control_poll_s=4 * LEASE),
    )

    assert timed.outcome is not None, "the run did not end by itself"
    assert len(timed.timeline.starts) == 1
    assert timed.wall_s < overrun
    assert timed.holds_lease
