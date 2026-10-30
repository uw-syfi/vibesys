"""A step's result is one of three typed values, each a closed record that survives its encoding."""

from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import TypeAdapter

import vs_core.api as core
from vs_core.api.recovery import (
    Cancelled,
    CancelReason,
    Failed,
    Finished,
    StepResult,
)
from vs_core.types.until import FailureReason
from vs_sim.api.testing import examples

_RESULT = TypeAdapter(StepResult)


@st.composite
def finished(draw: st.DrawFn) -> Finished:
    status = draw(st.sampled_from(core.ObservationStatus))
    return Finished(
        observation=core.Observation(
            event_id=core.EventId(root=draw(st.text("abc", min_size=1, max_size=4))),
            request_id=core.RequestId(root=draw(st.text("abc", min_size=1, max_size=4))),
            scope=core.Scope(owner=core.RunId(root="run"), generation=draw(st.integers(0, 3))),
            sequence=draw(st.integers(0, 9)),
            observed_at=draw(st.floats(0, 1e6)),
            status=status,
            terminal=draw(st.booleans()),
        )
    )


RESULTS = st.one_of(
    finished(),
    st.builds(Cancelled, reason=st.sampled_from(CancelReason)),
    st.just(Failed(reason=FailureReason.ATTEMPTS_EXHAUSTED)),
)


@settings(max_examples=examples(pr=8, nightly=100))
@given(result=RESULTS)
def test_a_step_result_round_trips_as_the_variant_it_was(result: StepResult) -> None:
    decoded = _RESULT.validate_json(_RESULT.dump_json(result))
    assert decoded == result
    assert type(decoded) is type(result)
