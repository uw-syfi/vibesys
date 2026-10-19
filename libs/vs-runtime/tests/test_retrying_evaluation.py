"""The backend measures again when a try fails for a reason that is not the candidate's.

The retry bound is the backend's declaration; core's hard cap lifts for no one; a candidate's
own failure is a result and is never measured again; a restarted host resumes the retrying.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from vs_core.api import MAX_SUBMISSIONS_PER_MEASUREMENT, MeasurementFailure
from vs_evaluation.api import (
    AvailabilitySnapshot,
    AvailabilityState,
    ContentDigest,
    CostClass,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvaluationStepResult,
    EvidenceFailureKind,
    EvidenceFingerprints,
    EvidenceKind,
    EvidenceOutcome,
    ExecutorObservation,
    ExecutorPoll,
    ExecutorRejectedError,
    PollPhase,
    ResourceRequirements,
    ReuseStatus,
    StageState,
    TrustedEvidence,
)
from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_runtime.api import EvaluationRetries, RetryingEvaluationExecutor
from vs_sim.api.testing import VirtualClock, run_virtual

_DIGEST = ContentDigest(value="a" * 64)


class Outcome(StrEnum):
    """How one try of a measurement ends."""

    SUCCEEDED = "succeeded"
    WORKLOAD = "workload"
    INFRASTRUCTURE = "infrastructure"
    AMBIGUOUS = "ambiguous"


_FAILURE_OF = {
    Outcome.WORKLOAD: MeasurementFailure.WORKLOAD,
    Outcome.INFRASTRUCTURE: MeasurementFailure.INFRASTRUCTURE,
    Outcome.AMBIGUOUS: MeasurementFailure.AMBIGUOUS,
}


def _terminal(outcome: Outcome) -> ExecutorObservation:
    if outcome is Outcome.SUCCEEDED:
        return ExecutorObservation(state=EvaluationState.SUCCEEDED)
    evidence = TrustedEvidence(
        evidence_id=hashlib.sha256(outcome.value.encode()).hexdigest(),
        evaluation_id="evaluation",
        stage_name="benchmark",
        kind=EvidenceKind.BENCHMARK,
        fingerprints=EvidenceFingerprints(
            candidate=_DIGEST, evaluator=_DIGEST, workload=_DIGEST, environment=_DIGEST
        ),
        trusted_inputs=_DIGEST,
        outcome=EvidenceOutcome.FAILED,
        failure_kind=EvidenceFailureKind(outcome.value),
        accepted_round=0,
    )
    return ExecutorObservation(
        state=EvaluationState.FAILED,
        failure="benchmark failed",
        stage_results=(
            EvaluationStepResult(
                name="benchmark",
                state=StageState.FAILED,
                failure="benchmark failed",
                result=evidence.model_dump(mode="json"),
            ),
        ),
    )


@dataclass
class ScriptedExecutor:
    """A Fake executor whose n-th submitted try ends as the script says (the last repeats)."""

    script: tuple[Outcome, ...]
    submitted: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    lose_next_submit: bool = False
    _ends: dict[str, Outcome] = field(default_factory=dict)

    async def availability(self, requirements: ResourceRequirements) -> AvailabilitySnapshot:
        del requirements
        return AvailabilitySnapshot(
            state=AvailabilityState.IMMEDIATE,
            in_flight=0,
            queue_depth=0,
            reuse_status=ReuseStatus.NONE,
            cost_class=CostClass.FREE,
            observed_at=0.0,
            fresh_for_s=1.0,
        )

    async def submit(self, request: EvaluationRequest, *, handle_id: str) -> None:
        del request
        if self.lose_next_submit:
            self.lose_next_submit = False
            return
        if handle_id not in self._ends:
            self.submitted.append(handle_id)
            self._ends[handle_id] = self.script[min(len(self._ends), len(self.script) - 1)]

    async def poll(self, handle_id: str) -> ExecutorPoll:
        outcome = self._ends.get(handle_id)
        if outcome is None:
            return ExecutorPoll(phase=PollPhase.UNSUBMITTED)
        return ExecutorPoll(phase=PollPhase.ENDED, terminal=_terminal(outcome))

    async def inspect_only(self, handle_id: str) -> ExecutorObservation | None:
        return await self.inspect(handle_id)

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        outcome = self._ends.get(handle_id)
        return None if outcome is None else _terminal(outcome)

    async def wait_for_change(self, handle_id: str, timeout_s: float) -> None:
        del handle_id, timeout_s

    async def cancel(self, handle_id: str) -> None:
        self.cancelled.append(handle_id)

    async def close(self) -> None:
        return None


def _request(key: str = "measure") -> EvaluationRequest:
    return EvaluationRequest(key=key, stages=(EvaluationStep(name="benchmark", payload={}),))


_BOUNDS = st.integers(min_value=1, max_value=MAX_SUBMISSIONS_PER_MEASUREMENT)
_RETRIES = st.tuples(_BOUNDS, _BOUNDS).map(
    lambda pair: EvaluationRetries(infrastructure_attempts=max(pair), ambiguous_attempts=min(pair))
)
_SCRIPTS = st.lists(st.sampled_from(Outcome), min_size=1, max_size=8).map(tuple)


async def _measure(executor: RetryingEvaluationExecutor) -> ExecutorObservation:
    """Poll until the measurement ends; the loop bound only guards against a hang."""
    await executor.submit(_request(), handle_id="h")
    for _ in range(4 * MAX_SUBMISSIONS_PER_MEASUREMENT):
        polled = await executor.poll("h")
        if polled.terminal is not None:
            return polled.terminal
    message = "the measurement never ended"
    raise AssertionError(message)


def _run(
    script: tuple[Outcome, ...], retries: EvaluationRetries
) -> tuple[ScriptedExecutor, ExecutorObservation]:
    inner = ScriptedExecutor(script)
    executor = RetryingEvaluationExecutor(inner, retries, InMemoryEvaluationNamespace())
    terminal = run_virtual(VirtualClock(), _measure(executor))
    return inner, terminal


@given(script=_SCRIPTS, retries=_RETRIES)
def test_the_backend_retries_only_what_is_not_the_candidates_and_only_within_its_bound(
    script: tuple[Outcome, ...], retries: EvaluationRetries
) -> None:
    inner, terminal = _run(script, retries)
    tries = len(inner.submitted)
    ended = [script[min(n, len(script) - 1)] for n in range(tries)]
    assert 1 <= tries <= MAX_SUBMISSIONS_PER_MEASUREMENT
    # every try the search never saw failed for a reason that is not the candidate's
    assert all(outcome in (Outcome.INFRASTRUCTURE, Outcome.AMBIGUOUS) for outcome in ended[:-1])
    # the search sees the last try only, and the backend stopped because it was done or out of tries
    assert terminal == _terminal(ended[-1])
    last = _FAILURE_OF.get(ended[-1])
    assert not retries.allows_another(last, tries=tries)


@given(
    script=st.lists(st.just(Outcome.WORKLOAD), min_size=1, max_size=3).map(tuple),
    retries=_RETRIES,
)
def test_a_candidate_failure_is_a_result_and_is_never_measured_again(
    script: tuple[Outcome, ...], retries: EvaluationRetries
) -> None:
    inner, terminal = _run(script, retries)
    assert inner.submitted == ["h"]
    assert terminal == _terminal(Outcome.WORKLOAD)


@given(retries=_RETRIES)
def test_a_run_of_infrastructure_failures_uses_exactly_the_declared_bound(
    retries: EvaluationRetries,
) -> None:
    inner, _ = _run((Outcome.INFRASTRUCTURE,), retries)
    assert len(inner.submitted) == retries.infrastructure_attempts
    inner, _ = _run((Outcome.AMBIGUOUS,), retries)
    assert len(inner.submitted) == retries.ambiguous_attempts


@given(retries=_RETRIES)
def test_a_retry_started_before_a_restart_is_not_lost(retries: EvaluationRetries) -> None:
    """The next try is recorded before it starts, so a restarted host starts it, not reports."""
    namespace = InMemoryEvaluationNamespace()
    inner = ScriptedExecutor((Outcome.INFRASTRUCTURE, Outcome.SUCCEEDED))
    first = RetryingEvaluationExecutor(inner, retries, namespace)

    async def before_restart() -> None:
        await first.submit(_request(), handle_id="h")
        inner.lose_next_submit = True  # the host dies as it starts the second try
        await first.poll("h")

    run_virtual(VirtualClock(), before_restart())
    restarted = RetryingEvaluationExecutor(inner, retries, namespace)
    terminal = run_virtual(VirtualClock(), _measure_resumed(restarted))
    if retries.infrastructure_attempts == 1:
        assert terminal == _terminal(Outcome.INFRASTRUCTURE)
    else:
        assert terminal == _terminal(Outcome.SUCCEEDED)
        assert inner.submitted == ["h", "h-try-2"]


async def _measure_resumed(executor: RetryingEvaluationExecutor) -> ExecutorObservation:
    for _ in range(4 * MAX_SUBMISSIONS_PER_MEASUREMENT):
        polled = await executor.poll("h")
        if polled.terminal is not None:
            return polled.terminal
    message = "the measurement never ended"
    raise AssertionError(message)


def test_cancel_reaches_the_try_in_flight_and_work_is_not_swapped_under_a_handle() -> None:
    retries = EvaluationRetries(infrastructure_attempts=3, ambiguous_attempts=2)
    inner = ScriptedExecutor((Outcome.INFRASTRUCTURE, Outcome.SUCCEEDED))
    executor = RetryingEvaluationExecutor(inner, retries, InMemoryEvaluationNamespace())

    async def story() -> None:
        await executor.submit(_request(), handle_id="h")
        await executor.poll("h")
        await executor.cancel("h")
        with pytest.raises(ExecutorRejectedError):
            await executor.submit(_request("other"), handle_id="h")

    run_virtual(VirtualClock(), story())
    assert inner.cancelled == ["h-try-2"]


@given(
    infrastructure=st.integers(min_value=MAX_SUBMISSIONS_PER_MEASUREMENT + 1, max_value=50),
)
def test_no_declaration_lifts_the_hard_cap(infrastructure: int) -> None:
    with pytest.raises(ValidationError, match="infrastructure_attempts"):
        EvaluationRetries(infrastructure_attempts=infrastructure, ambiguous_attempts=1)
    with pytest.raises(ValidationError, match="ambiguous_attempts"):
        EvaluationRetries(infrastructure_attempts=1, ambiguous_attempts=infrastructure)


def test_the_declaration_has_no_defaults_no_unknown_keys_and_no_harder_ambiguity() -> None:
    with pytest.raises(ValidationError):
        EvaluationRetries.model_validate({"infrastructure_attempts": 2})
    with pytest.raises(ValidationError, match="extra"):
        EvaluationRetries.model_validate(
            {"infrastructure_attempts": 2, "ambiguous_attempts": 1, "retries": 3}
        )
    with pytest.raises(ValidationError, match="ambiguous_attempts"):
        EvaluationRetries(infrastructure_attempts=1, ambiguous_attempts=2)


@dataclass
class _UnavailableEnvironmentExecutor(ScriptedExecutor):
    """Refuses every try: the run environment cannot run Slurm work (its probe failed)."""

    def refusal(self) -> ExecutorObservation:
        return ExecutorObservation(
            state=EvaluationState.FAILED,
            failure="Slurm `remote_workspace_root` startup probe failed: probe job 5000 "
            "exited with code 1",
            run_environment_unavailable=True,
        )

    async def poll(self, handle_id: str) -> ExecutorPoll:
        if handle_id not in self.submitted:
            return ExecutorPoll(phase=PollPhase.UNSUBMITTED)
        return ExecutorPoll(phase=PollPhase.ENDED, terminal=self.refusal())

    async def inspect(self, handle_id: str) -> ExecutorObservation | None:
        return self.refusal() if handle_id in self.submitted else None


@pytest.mark.xfail(strict=True, reason="F1: no outcome says the run environment is unavailable")
@given(retries=_RETRIES)
def test_a_measurement_refused_because_the_run_environment_is_unavailable_is_never_retried(
    retries: EvaluationRetries,
) -> None:
    inner = _UnavailableEnvironmentExecutor((Outcome.SUCCEEDED,))
    executor = RetryingEvaluationExecutor(inner, retries, InMemoryEvaluationNamespace())

    terminal = run_virtual(VirtualClock(), _measure(executor))

    assert inner.submitted == ["h"]
    assert terminal.run_environment_unavailable is True
    assert not MeasurementFailure.RUN_ENVIRONMENT_UNAVAILABLE.retryable
