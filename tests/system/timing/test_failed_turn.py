"""A turn whose process dies halts the run, but not before what the other turns returned is kept.

Two turns run at once on the virtual clock; one dies when its time is up. The loop raises the
executor's error (the failure is loud), and every turn that had handed its observation back by
then has it committed in the run's record. (Overlap up to the cap, completion order and host
death are in the neighbouring tests; an agent tool call admitted while a publication awaits is in
``tests/system/agent_tools``.)
"""

from __future__ import annotations

from hypothesis import Phase, example, given, settings
from hypothesis import strategies as st
from tests.system.skeleton_timing import (
    Plan,
    StaggeredConfig,
    StaggeredStrategy,
    TurnDiedError,
    run_timed,
)

import vs_core.api as core
from vs_sim.api.testing import Depth, depth, examples

_NIGHTLY = depth() is Depth.NIGHTLY
_SETTINGS = settings(
    max_examples=examples(pr=1, nightly=8),
    phases=(Phase.explicit, Phase.generate) if _NIGHTLY else (Phase.explicit,),
    deadline=None,
    derandomize=True,
    database=None,
)


@_SETTINGS
@example(durations=[10.0, 40.0], dying=1)
@example(durations=[40.0, 10.0], dying=0)
@example(durations=[20.0, 20.0], dying=1)
@given(
    durations=st.lists(st.integers(min_value=5, max_value=60).map(float), min_size=2, max_size=2),
    dying=st.integers(min_value=0, max_value=1),
)
def test_a_dying_turn_halts_the_run_after_the_returned_turns_are_committed(
    durations: list[float], dying: int
) -> None:
    timed = run_timed(
        StaggeredStrategy(config=StaggeredConfig(workers=2)),
        Plan(
            duration=lambda worker, _ordinal: durations[worker],
            fails=lambda worker, _ordinal: worker == dying,
            max_parallel=2,
        ),
    )

    assert isinstance(timed.failure, TurnDiedError)
    assert timed.outcome is None
    records = {item.request_id.root: item for item in timed.core.records.records}
    for request_id in timed.timeline.returned:
        assert records[request_id].phase == core.RequestPhase.COMPLETED, request_id
