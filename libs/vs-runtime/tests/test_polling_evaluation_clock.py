"""The polling executor stamps availability with the clock it was given."""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import ResourceRequirements
from vs_runtime.api import PollingEvaluationExecutor
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import ManualClock, VirtualClock, run_virtual


@given(start=st.floats(min_value=0, max_value=1e9), step=st.floats(min_value=0, max_value=1e6))
def test_availability_is_observed_at_the_injected_clock_time(start: float, step: float) -> None:
    clock = ManualClock(start)
    executor = PollingEvaluationExecutor(
        FakeEvaluation(run_id="run-1"),
        FakeWorkspaces(FakeWorkspace(), supports_parallel_candidates=True),
        clock=clock,
    )

    async def observe() -> tuple[float, float]:
        first = await executor.availability(ResourceRequirements())
        clock.advance(step)
        second = await executor.availability(ResourceRequirements())
        return first.observed_at, second.observed_at

    first, second = run_virtual(VirtualClock(), observe())
    assert (first, second) == (start, start + step)
