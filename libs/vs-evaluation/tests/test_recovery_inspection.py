"""The durable Fake owner keeps observations across client replacement."""

from hypothesis import example, given
from hypothesis import strategies as st

from vs_evaluation.api import (
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    PollPhase,
    is_finished,
)
from vs_evaluation.api.testing import FakeEvaluationExecutor
from vs_sim.api.testing import ManualClock, Sim


@example(states=[EvaluationState.RUNNING, EvaluationState.FAILED, EvaluationState.SUCCEEDED])
@example(states=[EvaluationState.SUPERSEDED])
@given(states=st.lists(st.sampled_from(EvaluationState), min_size=1, max_size=12))
def test_recovery_projects_the_same_retained_lifecycle_without_new_effects(
    states: list[EvaluationState],
) -> None:
    sim = Sim(seed=0)
    original = FakeEvaluationExecutor(ManualClock())
    request = EvaluationRequest(
        key="measurement", stages=(EvaluationStep(name="build", payload={}),)
    )

    async def story() -> None:
        assert (await original.recover("handle")).phase is PollPhase.UNSUBMITTED
        await original.submit(request, handle_id="handle")
        for state in states:
            original.set_state(
                "handle",
                state,
                failure="execution failed" if state is EvaluationState.FAILED else None,
            )
            fresh = FakeEvaluationExecutor(ManualClock(), backend=original.backend)
            retained = await original.inspect("handle")
            assert retained is not None
            observed = await fresh.recover("handle")
            if is_finished(retained.state):
                assert observed.phase is PollPhase.ENDED
                assert observed.terminal == retained
            elif retained.state is EvaluationState.QUEUED:
                assert observed.phase is PollPhase.QUEUED
            else:
                assert observed.phase is PollPhase.RUNNING
            assert observed == await original.poll("handle")
            assert await fresh.recover("handle") == observed
            assert len(original.submissions) == 1
            assert not fresh.cancellations

    sim.run(story())
