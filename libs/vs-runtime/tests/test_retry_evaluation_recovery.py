"""Recovery observes the durable physical try; it never advances the retry policy."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_evaluation.api import PollPhase
from vs_evaluation.api.testing import FakeEvaluationExecutor, InMemoryEvaluationNamespace
from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_runtime.api import EvaluationRetries, RetryingEvaluationExecutor
from vs_sim.api.testing import ManualClock, Sim

from .test_retrying_evaluation import Outcome, ScriptedExecutor, _request, _terminal

if TYPE_CHECKING:
    from pathlib import PurePosixPath

    from pydantic import BaseModel


class FaultingNamespace(InMemoryEvaluationNamespace):
    """The namespace contract with the shared fault gate at each atomic write."""

    def __init__(self, gate: FaultGate) -> None:
        super().__init__()
        self.gate = gate

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        self.gate.around(
            Boundary.DURABLE_WRITE, "retry", partial(super().save, relative_path, model)
        )


@pytest.mark.regression
@example(attempt=2, repeats=2, outcome=Outcome.INFRASTRUCTURE)
@given(attempt=st.integers(1, 3), repeats=st.integers(1, 5), outcome=st.sampled_from(Outcome))
def test_recovery_observes_the_recorded_try_without_advancing_it(
    attempt: int, repeats: int, outcome: Outcome
) -> None:
    sim = Sim(seed=0)
    namespace = InMemoryEvaluationNamespace()
    inner = FakeEvaluationExecutor(ManualClock())
    retries = EvaluationRetries(infrastructure_attempts=4, ambiguous_attempts=4)

    async def story() -> None:
        first = RetryingEvaluationExecutor(inner, retries, namespace)
        await first.submit(_request(), handle_id="h")
        for _ in range(attempt - 1):
            inner.set_observation(
                inner.submissions[-1].handle_id, _terminal(Outcome.INFRASTRUCTURE)
            )
            assert (await first.poll("h")).phase is PollPhase.QUEUED
        inner.set_observation(inner.submissions[-1].handle_id, _terminal(outcome))
        submissions = list(inner.submissions)
        for _ in range(repeats):
            reopened = FakeEvaluationExecutor(ManualClock(), backend=inner.backend)
            recovered = RetryingEvaluationExecutor(reopened, retries, namespace)
            observed = await recovered.recover("h")
            assert observed.phase is PollPhase.ENDED
            assert observed.terminal == _terminal(outcome)
            assert inner.submissions == submissions
        # Observation did not consume an ordinal. Only the legacy dispatch path
        # can start a following try, under its original retry allowance.
        if outcome is Outcome.INFRASTRUCTURE:
            assert (await first.poll("h")).phase is PollPhase.QUEUED
            assert inner.submissions[-1].handle_id == f"h-try-{attempt + 1}"

    sim.run(story())


@pytest.mark.regression
@pytest.mark.parametrize("ordinal", [1, 2])
@pytest.mark.parametrize("delivered", [False, True])
async def test_crashing_after_retry_intent_or_delivery_never_dispatches_during_recovery(
    ordinal: int, *, delivered: bool
) -> None:
    gate = FaultGate(
        FaultPlan(
            seed=0,
            rules=(
                FaultRule(
                    boundary=Boundary.EXECUTOR_REQUEST if delivered else Boundary.DURABLE_WRITE,
                    target="delivery" if delivered else "retry",
                    at=ordinal,
                    fault=HostFault.CRASH_AFTER,
                ),
            ),
        )
    )
    namespace = FaultingNamespace(gate)
    inner = FakeEvaluationExecutor(ManualClock())
    retries = EvaluationRetries(infrastructure_attempts=3, ambiguous_attempts=3)
    first = RetryingEvaluationExecutor(inner, retries, namespace)

    async def deliver() -> None:
        await gate.around_async(
            Boundary.EXECUTOR_REQUEST,
            "delivery",
            partial(first.submit, _request(), handle_id="h"),
        )
        inner.set_observation("h", _terminal(Outcome.INFRASTRUCTURE))
        await gate.around_async(Boundary.EXECUTOR_REQUEST, "delivery", partial(first.poll, "h"))

    with pytest.raises(HostCrashError):
        await deliver()
    gate.heal()
    submitted = list(inner.submissions)
    writes = len(gate.calls)
    for _ in range(3):
        recovered = RetryingEvaluationExecutor(inner, retries, namespace)
        observed = await gate.around_async(
            Boundary.EXECUTOR_REQUEST, "recovery", partial(recovered.recover, "h")
        )
        expected = PollPhase.QUEUED if delivered else PollPhase.UNSUBMITTED
        assert observed.phase is expected
        assert inner.submissions == submitted
    assert all(call.boundary is Boundary.EXECUTOR_REQUEST for call in gate.calls[writes:])


@pytest.mark.regression
async def test_recovery_requires_the_inner_owners_authoritative_inspection() -> None:
    inner = ScriptedExecutor((Outcome.SUCCEEDED,))
    recovered = RetryingEvaluationExecutor(
        inner,
        EvaluationRetries(infrastructure_attempts=2, ambiguous_attempts=1),
        InMemoryEvaluationNamespace(),
    )
    observed = await recovered.recover("h")
    assert observed.phase is PollPhase.UNKNOWN
    assert "authoritative recovery" in observed.detail
    assert not inner.submitted
