"""A stop reaches a terminal run without starting work, wherever it finds the run busy.

The shell joins core to a host whose bound on a stop is a grace period (``GRACE_S``, the host's
``STOP_GRACE_S``): work core can cancel (an agent turn) ends within a few control polls, work it
cannot (a queued job being observed) is waited for until the grace bound, after which the host
cancels the loop and closing the evaluation executors cancels every job still on the cluster.
Whatever the stop finds in flight, no new turn and no new job starts after it.

Each scenario is the staggered or probing skeleton search on the virtual clock
(``tests/support/skeleton_timing.py``): a turn lasts virtual seconds, a held job never ends by
itself, and the stop arrives at a drawn instant.

Past bug these guard: a stop used to wait for the running turn, then for the host's 60 s grace
bound (#1370): the obeyed-cancel bound below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st
from tests.support.skeleton_strategy import SkeletonStrategy
from tests.support.skeleton_timing import (
    GRACE_S,
    Plan,
    StaggeredConfig,
    StaggeredStrategy,
    Timed,
    TurnTimeline,
    run_timed,
)

from vs_core.api import RunStatus, StopMode
from vs_sim.api.testing import Depth, depth, examples

#: A stop during a turn the agent obeys a cancel for is acted on within this many virtual seconds.
STOP_BOUND_S = 5.0

_MODES = st.sampled_from(StopMode)

if TYPE_CHECKING:
    from collections.abc import Callable

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


def _one_worker() -> StaggeredStrategy:
    return StaggeredStrategy(config=StaggeredConfig(workers=1))


def _into_the_turn(seconds: float) -> Callable[[TurnTimeline, object], bool]:
    """A stop condition: ``seconds`` after the first turn began."""

    def reached(timeline: TurnTimeline, _cluster: object) -> bool:
        return bool(timeline.starts) and timeline.clock.now() >= timeline.starts[0] + seconds

    return reached


def _nothing_started_after_the_stop(timed: Timed) -> None:
    assert timed.stopped_at is not None
    assert not timed.timeline.started_after(timed.stopped_at), "a turn began after the stop"
    assert all(at <= timed.stopped_at for at in timed.cluster.submitted_at), (
        "a job was submitted after the stop"
    )


def test_a_stop_during_the_baseline_measurement_cancels_its_job_and_starts_no_work() -> None:
    """The queued job is not the loop's to cancel until the host gives up: the host's grace bound
    ends the loop, and closing the evaluation executors cancels the job.
    """
    timed = run_timed(
        SkeletonStrategy.baseline_probe(),
        Plan(hold_from=0, stop_when=lambda _timeline, cluster: len(cluster.submissions) >= 1),
    )

    assert timed.stopped_at is not None
    assert timed.gave_up
    assert GRACE_S <= timed.ended_at - timed.stopped_at <= GRACE_S + 1.0
    assert len(timed.cluster.submissions) == 1
    assert sorted((*timed.cancelled_during, *timed.cancelled_by_exit)) == timed.cluster.submissions
    assert timed.timeline.starts == []
    _nothing_started_after_the_stop(timed)


def test_a_cancel_stop_landing_on_a_poll_of_a_queued_job_cancels_its_job_and_starts_no_work() -> (
    None
):
    """The same scenario as the baseline test, with a cancel stop: the stop cancels the job."""
    timed = run_timed(
        SkeletonStrategy.baseline_probe(),
        Plan(
            hold_from=0,
            mode=StopMode.CANCEL,
            stop_when=lambda _timeline, cluster: len(cluster.submissions) >= 1,
        ),
    )

    assert timed.stopped_at is not None
    assert timed.outcome is not None
    assert timed.outcome.status is RunStatus.TERMINAL
    assert len(timed.cluster.submissions) == 1
    assert sorted((*timed.cancelled_during, *timed.cancelled_by_exit)) == timed.cluster.submissions
    assert timed.timeline.starts == []
    _nothing_started_after_the_stop(timed)


@pytest.mark.parametrize("mode", list(StopMode))
def test_a_stop_during_a_candidate_evaluation_cancels_its_job_and_starts_no_new_work(
    mode: StopMode,
) -> None:
    timed = run_timed(
        _one_worker(),
        Plan(
            hold_from=0,
            mode=mode,
            stop_when=lambda _timeline, cluster: len(cluster.submissions) >= 1,
        ),
    )

    assert timed.outcome is not None, "the stop was not acted on before the host's grace bound"
    assert timed.outcome.status is RunStatus.TERMINAL
    assert timed.stopped_at is not None
    assert timed.ended_at - timed.stopped_at <= STOP_BOUND_S
    assert len(timed.cluster.submissions) == 1
    assert sorted((*timed.cancelled_during, *timed.cancelled_by_exit)) == timed.cluster.submissions
    _nothing_started_after_the_stop(timed)


@_SETTINGS
@example(mode=StopMode.CANCEL, seconds=0.5)
@example(mode=StopMode.DRAIN, seconds=10.0)
@given(mode=_MODES, seconds=st.floats(min_value=0.5, max_value=19.0))
def test_a_stop_mid_turn_cancels_the_turn_within_the_bound_and_starts_no_evaluation(
    mode: StopMode, seconds: float
) -> None:
    timed = run_timed(
        _one_worker(),
        Plan(duration=lambda _worker, _ordinal: 20.0, mode=mode, stop_when=_into_the_turn(seconds)),
    )

    assert timed.outcome is not None
    assert timed.outcome.status is RunStatus.TERMINAL
    assert timed.stopped_at is not None
    (turn,) = timed.timeline.spans
    assert turn.cancelled, "the stop did not interrupt the turn"
    assert turn.end - timed.stopped_at <= STOP_BOUND_S
    assert timed.ended_at - timed.stopped_at <= STOP_BOUND_S
    assert timed.cluster.submissions == []
    _nothing_started_after_the_stop(timed)


@pytest.mark.parametrize("mode", list(StopMode))
def test_a_stop_while_two_turns_overlap_ends_both_and_dispatches_nothing_stale(
    mode: StopMode,
) -> None:
    """The second turn's request is prepared behind the first's when the stop comes.

    Core ends a prepared request of an attempt the stop closed instead of dispatching it: a
    dispatch would act in an episode that is over, and the contract refuses it.
    """
    timed = run_timed(
        StaggeredStrategy(config=StaggeredConfig(workers=2)),
        Plan(duration=lambda _worker, _ordinal: 20.0, mode=mode, stop_when=_into_the_turn(5.0)),
    )

    assert timed.outcome is not None
    assert timed.outcome.status is RunStatus.TERMINAL
    assert timed.stopped_at is not None
    assert timed.ended_at - timed.stopped_at <= STOP_BOUND_S
    assert timed.cluster.submissions == []
    _nothing_started_after_the_stop(timed)


@pytest.mark.parametrize("mode", list(StopMode))
def test_a_stuck_turn_ends_at_the_grace_bound_and_starts_no_evaluation(mode: StopMode) -> None:
    """A turn that ignores the cancel never ends: only the host's grace bound ends the run."""
    timed = run_timed(
        _one_worker(),
        Plan(
            duration=lambda _worker, _ordinal: 100_000.0,
            obeys_cancel=False,
            mode=mode,
            stop_when=_into_the_turn(5.0),
        ),
    )

    assert timed.gave_up
    assert timed.stopped_at is not None
    assert GRACE_S <= timed.ended_at - timed.stopped_at <= GRACE_S + 1.0
    assert timed.cluster.submissions == []
    _nothing_started_after_the_stop(timed)


@_SETTINGS
@example(mode=StopMode.DRAIN, seconds=1.0)
@example(mode=StopMode.CANCEL, seconds=14.0)
@given(mode=_MODES, seconds=st.floats(min_value=0.5, max_value=15.0))
def test_a_turn_that_ends_after_a_stop_starts_no_further_work(
    mode: StopMode, seconds: float
) -> None:
    """The turn finishes on its own within the grace period; what it would be followed by
    (the evaluation of its change) is never submitted.
    """
    timed = run_timed(
        _one_worker(),
        Plan(
            duration=lambda _worker, _ordinal: 20.0,
            obeys_cancel=False,
            mode=mode,
            stop_when=_into_the_turn(seconds),
        ),
    )

    assert timed.outcome is not None
    assert timed.outcome.status is RunStatus.TERMINAL
    (turn,) = timed.timeline.spans
    assert not turn.cancelled
    assert timed.stopped_at is not None
    assert turn.end > timed.stopped_at, "the stop came after the turn ended"
    assert timed.cluster.submissions == []
    _nothing_started_after_the_stop(timed)
