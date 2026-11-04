"""The polling executor runs one request at a time, the capacity it reports.

Every request it runs executes on the host's one set of devices (a GPU, a Neuron
device), so two at once compete for them: on Trainium the second runtime cannot open
the cores the first holds, and both measurements fail. A request submitted while
another runs stays queued, starts no stage and opens no workspace, and runs once the
first has ended.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import given
from hypothesis import strategies as st

from vs_evaluation.api import (
    ContentDigest,
    EvaluationRequest,
    EvaluationState,
    EvaluationStep,
    EvidenceFingerprints,
    EvidenceKind,
    ExecutorObservation,
    SemanticEvaluationStage,
)
from vs_runtime.api import AccuracyEvaluation, PollingEvaluationExecutor
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import VirtualClock, run_virtual, yield_until

_TERMINAL = frozenset({EvaluationState.SUCCEEDED, EvaluationState.FAILED, EvaluationState.CANCELED})
_IDLE_S = 1.0
"""A virtual sleep: it returns only once the loop had nothing else to run."""


def _request(snapshot: str) -> EvaluationRequest:
    digest = ContentDigest.sha256(b"same")
    fingerprints = EvidenceFingerprints(
        candidate=digest, evaluator=digest, workload=digest, environment=digest
    )
    stage = SemanticEvaluationStage(
        snapshot=snapshot, kind=EvidenceKind.ACCURACY, fingerprints=fingerprints
    )
    return EvaluationRequest(
        key=snapshot,
        stages=(EvaluationStep(name="accuracy", payload=stage.model_dump(mode="json")),),
    )


async def _started(
    evaluation: FakeEvaluation, clock: VirtualClock, handles: list[str]
) -> PollingEvaluationExecutor:
    """An executor with one request submitted per handle, in order, each of its own snapshot."""
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/workspace")), supports_parallel_candidates=True
    )
    executor = PollingEvaluationExecutor(evaluation, workspaces, clock=clock)
    for handle in handles:
        snapshot = await workspaces.root.snapshot(handle)
        await executor.submit(_request(snapshot), handle_id=handle)
    return executor


async def _state(executor: PollingEvaluationExecutor, handle: str) -> EvaluationState | None:
    observed = await executor.inspect(handle)
    return None if observed is None else observed.state


async def _terminal(executor: PollingEvaluationExecutor, handle: str) -> ExecutorObservation:
    observed = await yield_until(
        lambda: executor.inspect(handle),
        lambda seen: seen is not None and seen.state in _TERMINAL,
        f"a terminal observation of {handle}",
    )
    assert observed is not None
    return observed


@given(count=st.integers(min_value=2, max_value=5), seed=st.integers(0, 2**16))
def test_requests_submitted_together_run_one_at_a_time(count: int, seed: int) -> None:
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
    gates = [evaluation.gate("accuracy", call) for call in range(count)]
    handles = [f"handle-{index}" for index in range(count)]
    clock = VirtualClock()

    async def scenario() -> list[tuple[int, list[EvaluationState | None]]]:
        executor = await _started(evaluation, clock, handles)
        seen: list[tuple[int, list[EvaluationState | None]]] = []
        for gate in gates:
            await gate.entered.wait()
            await clock.sleep(_IDLE_S)
            states = [await _state(executor, handle) for handle in handles]
            seen.append((sum(g.entered.is_set() for g in gates), states))
            gate.release()
            for handle, state in zip(handles, states, strict=True):
                if state is EvaluationState.RUNNING:
                    await _terminal(executor, handle)
        await executor.close()
        return seen

    seen = run_virtual(clock, scenario(), schedule_seed=seed)

    # While the n-th request runs, only n requests ever reached a stage: the earlier ones
    # ended, the running one is the only one running, and every later one is still queued.
    for running, (entered, states) in enumerate(seen):
        assert entered == running + 1
        assert states.count(EvaluationState.RUNNING) == 1
        assert states.count(EvaluationState.QUEUED) == count - running - 1
    assert len(evaluation.accuracy_calls) == count


def test_a_queued_request_cancels_without_running_or_disturbing_the_running_one() -> None:
    evaluation = FakeEvaluation(default_accuracy=AccuracyEvaluation(executed=True))
    running = evaluation.gate("accuracy", 0)
    clock = VirtualClock()

    async def scenario() -> tuple[ExecutorObservation, ExecutorObservation, int]:
        executor = await _started(evaluation, clock, ["running", "queued"])
        await running.entered.wait()
        await executor.cancel("queued")
        cancelled = await _terminal(executor, "queued")
        await clock.sleep(_IDLE_S)
        calls_while_running = len(evaluation.accuracy_calls)
        running.release()
        finished = await _terminal(executor, "running")
        await executor.close()
        return cancelled, finished, calls_while_running

    cancelled, finished, calls_while_running = run_virtual(clock, scenario())

    assert cancelled.state is EvaluationState.CANCELED
    assert cancelled.stage_results == ()
    assert finished.state is EvaluationState.SUCCEEDED
    assert calls_while_running == 1
    assert len(evaluation.accuracy_calls) == 1
