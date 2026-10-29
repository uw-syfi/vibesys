"""An internal quota hold is read while a turn runs, not only when a request finishes.

Turns last minutes, so a loop that read its controls only between requests would pause long
after the operator asked. The pause is acted on within one control poll and the turn goes on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st
from tests.system.skeleton_timing import (
    Plan,
    StaggeredConfig,
    StaggeredStrategy,
    TurnTimeline,
    run_timed,
)

from vs_sim.api.testing import Depth, depth, examples

_TURN_S = 100.0

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


def _into_the_turn(seconds: float) -> Callable[[TurnTimeline, object], bool]:
    def reached(timeline: TurnTimeline, _cluster: object) -> bool:
        return bool(timeline.starts) and timeline.clock.now() >= timeline.starts[0] + seconds

    return reached


@_SETTINGS
@example(poll=7.0, seconds=20.0)
@given(poll=st.sampled_from([1.0, 7.0, 30.0]), seconds=st.floats(min_value=1.0, max_value=90.0))
def test_a_quota_hold_during_a_long_turn_is_acted_on_within_one_control_poll(
    poll: float, seconds: float
) -> None:
    timed = run_timed(
        StaggeredStrategy(config=StaggeredConfig(workers=1)),
        Plan(
            duration=lambda _worker, _ordinal: _TURN_S,
            quota_hold_when=_into_the_turn(seconds),
            control_poll_s=poll,
        ),
    )

    assert timed.pause_requested_at is not None
    assert timed.paused_at is not None
    assert timed.paused_at - timed.pause_requested_at <= poll
    # The turn was still running when the run paused, and the run goes on after the resume.
    (turn,) = timed.timeline.spans
    assert timed.paused_at < turn.end
    assert timed.outcome is not None
    assert timed.outcome.result is not None
    assert timed.outcome.result.outcome == "success"
