"""Supplied input timestamps advance the kernel clock independently of batching."""

from hypothesis import example, given
from hypothesis import strategies as st

import vs_core.api as core


@given(
    now=st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False),
    received=st.lists(
        st.floats(min_value=0, max_value=100, allow_nan=False, allow_infinity=False),
        max_size=12,
    ),
)
@example(now=0.0, received=[100.0])
@example(now=5.0, received=[20.0, 10.0, 30.0])
@example(now=5.0, received=[])
@example(now=50.0, received=[10.0, 20.0])
def test_steering_and_single_inputs_advance_time_to_the_same_monotonic_maximum(
    now: float, received: list[float]
) -> None:
    state = core.initial_state()
    state = state.model_copy(update={"run": state.run.model_copy(update={"now_at": now})})
    invocation = core.InvocationRef(
        session_id=core.SessionId(root="session"),
        invocation_id=core.InvocationId(root="invocation"),
        generation=0,
    )
    inputs = tuple(
        core.SessionInput(
            input_id=core.InputId(root=f"input-{index}"),
            target=core.InvocationInputTarget(invocation=invocation),
            artifact=core.ArtifactRef(artifact_id=core.ArtifactId(root="note"), digest="digest"),
            received_at=at,
            sequence=index,
        )
        for index, at in enumerate(received)
    )
    bulk = core.SteerReceived(invocation=invocation, inputs=inputs)
    bulk_result = core.trace_step(
        state,
        bulk,
        core.ReducerTrace(
            frames=(core.TraceFrame(signal=bulk, change=core.SessionsChange(state=state.sessions)),)
        ),
    )
    singleton_state = state
    for session_input in inputs:
        singleton = core.SessionInputReceived(input=session_input)
        singleton_state = core.trace_step(
            singleton_state,
            singleton,
            core.ReducerTrace(
                frames=(
                    core.TraceFrame(
                        signal=singleton, change=core.SessionsChange(state=singleton_state.sessions)
                    ),
                )
            ),
        ).state
    expected = max([now, *received])
    assert core.project(singleton_state).run.now_at == expected
    assert core.project(bulk_result.state).run.now_at == expected
    assert bulk_result.state.run.now_at == singleton_state.run.now_at
    assert state.run.now_at == now
