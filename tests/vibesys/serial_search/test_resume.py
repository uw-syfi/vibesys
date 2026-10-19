"""A serial search's state survives its checkpoint: what resumes is what was saved.

`SerialRun.state` is what the root checkpoints and `SerialRun.resumed` what a restart on changed
code builds from it, through JSON as the persisted state is. The resumed run starts the round
after the last, names the same best and ends when the saved run would.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.serial_search._host import TERMS
from tests.vibesys.serial_search.test_rounds import (
    INPUT_REVISION,
    closed,
    done,
    rounds_drawn,
    run_of,
)

from vibesys.hypothesis import HypothesisConfig
from vibesys.serial_search.api import SerialState
from vs_sim.api.testing import examples


@settings(max_examples=examples(pr=30, nightly=300))
@given(drawn=rounds_drawn, limit=st.integers(1, 8), stopping=st.booleans())
def test_a_resumed_run_starts_the_next_round_with_the_same_best(
    *, drawn: list[tuple[bool, bool, float]], limit: int, stopping: bool
) -> None:
    run = run_of(limit)
    for index, (counted, eligible, value) in enumerate(drawn, start=1):
        run.finished(
            done(index, counted=counted, kept=closed(index, eligible=eligible, value=value)),
            f"flow:{index:06d}/#1",
            stopping=stopping,
        )
    saved = SerialState.model_validate_json(run.state().model_dump_json())
    resumed = type(run).resumed(HypothesisConfig(max_rounds=limit), INPUT_REVISION, TERMS, saved)

    assert resumed.counted == run.counted
    assert resumed.start() == run.start()
    assert resumed.leading() == run.leading()
    assert resumed.over(stopping=False) == run.over(stopping=False)
    assert resumed.state() == run.state()
