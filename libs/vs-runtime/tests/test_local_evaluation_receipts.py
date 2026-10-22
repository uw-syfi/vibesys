"""Fresh owners retain local results and never redispatch uncertain stages or cleanup."""

from __future__ import annotations

from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_evaluation.api import (
    EvaluationState,
    ExecutorCancellationUnknownError,
    ExecutorObservation,
    PollPhase,
    TrustedEvidence,
)
from vs_evaluation.api.testing import InMemoryEvaluationNamespace
from vs_evaluator_protocol.api import PartialMeasurement, Progress
from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_runtime.api import BenchmarkEvaluation, BenchmarkFailureKind, PollingEvaluationExecutor
from vs_runtime.api.replay import (
    EvaluationReceiptUnconfirmedError,
    EvaluationRecoveryBlockedError,
    LocalEvaluationReceipts,
)
from vs_runtime.api.testing import FakeEvaluation, FakeWorkspace, FakeWorkspaces
from vs_sim.api.testing import ManualClock, VirtualClock, run_virtual, yield_until

from .test_evaluation_executor_contract import _request

if TYPE_CHECKING:
    from vs_proto.api import evaluation_receipts


class ReceiptWrites(InMemoryEvaluationNamespace):
    """The namespace contract with scheduled lost write acknowledgements."""

    def __init__(self, crash: int | None, *, before: bool = False) -> None:
        super().__init__()
        self.before = before
        self.gate = FaultGate(
            FaultPlan(
                seed=0,
                rules=()
                if crash is None
                else (
                    FaultRule(
                        boundary=Boundary.DURABLE_WRITE,
                        target="local",
                        at=crash,
                        fault=HostFault.CRASH_AFTER,
                    ),
                ),
            )
        )
        self.crashed = False

    def write_bytes(self, relative_path: str | PurePosixPath, contents: bytes) -> None:
        write = partial(super().write_bytes, relative_path, contents)
        if not str(relative_path).endswith(".pb"):
            write()
            return
        try:
            if self.before:
                self.gate.around(Boundary.DURABLE_WRITE, "local", lambda: None)
                write()
            else:
                self.gate.around(Boundary.DURABLE_WRITE, "local", write)
        except HostCrashError as error:
            self.crashed = True
            # A lost acknowledgement has the same boundary failure as the real
            # Project byte store. FaultGate remains the only fault mechanism.
            raise OSError(str(error)) from error

    async def interrupted(self) -> bool:
        return self.crashed


def executor(
    namespace: InMemoryEvaluationNamespace,
) -> tuple[PollingEvaluationExecutor, FakeEvaluation, FakeWorkspaces]:
    workspaces = FakeWorkspaces(
        FakeWorkspace(path=Path("/workspace")), supports_parallel_candidates=True
    )
    evaluation = FakeEvaluation()
    return (
        PollingEvaluationExecutor(
            evaluation, workspaces, clock=ManualClock(), receipts=LocalEvaluationReceipts(namespace)
        ),
        evaluation,
        workspaces,
    )


async def assert_blocked(executor: PollingEvaluationExecutor) -> None:
    with pytest.raises(EvaluationRecoveryBlockedError, match="Recovery blocked") as blocked:
        await executor.recover("local")
    assert blocked.value.block.environment == "local"
    assert blocked.value.block.outstanding == ["local"]
    assert "termination" in blocked.value.block.prerequisite
    with pytest.raises(EvaluationRecoveryBlockedError):
        await executor.cancel("local")
    with pytest.raises(EvaluationRecoveryBlockedError):
        await executor.wait_for_change("local", 1)


@pytest.mark.regression
@pytest.mark.parametrize("crash", range(1, 15))
async def test_every_local_receipt_crash_recovers_results_or_blocks_visibly(crash: int) -> None:
    namespace = ReceiptWrites(crash)
    old, _, workspaces = executor(namespace)
    fresh, evaluation, replacement = executor(namespace)
    snapshot = await workspaces.root.snapshot("candidate")
    try:
        try:
            await old.submit(_request(snapshot), handle_id="local")
        except EvaluationReceiptUnconfirmedError:
            assert namespace.crashed
        await yield_until(namespace.interrupted, bool, "the scheduled receipt interruption")
        namespace.gate.heal()
        retained = LocalEvaluationReceipts(namespace).load("local")
        assert retained is not None
        for _ in range(3):
            if retained.cleaned:
                recovered = await fresh.recover("local")
                assert recovered.phase is PollPhase.ENDED
                assert recovered.terminal is not None
                assert recovered.terminal.state is EvaluationState.SUCCEEDED
                assert len(recovered.terminal.stage_results) == 2
            else:
                await assert_blocked(fresh)
        assert not replacement.candidates
        assert not evaluation.accuracy_calls
        if crash > 7:
            assert retained.started_stages
        if crash >= 9:
            assert retained.observation is not None
            assert retained.observation.stage_results
        if crash >= 4:
            assert retained.cleanup_required
        if crash >= 5:
            assert retained.workspace
        if crash == 13:
            assert retained.cleanup_started
            assert not retained.cleaned
            assert not workspaces.candidates[0].discarded
        if crash == 14:
            assert retained.cleaned
            assert workspaces.candidates[0].discarded
    finally:
        await old.close()
        await fresh.close()


@pytest.mark.regression
@example(stage_count=2)
@given(stage_count=st.integers(min_value=1, max_value=5))
def test_completed_stage_results_survive_a_fresh_owner_before_any_replay_seal(
    stage_count: int,
) -> None:
    async def case() -> None:
        namespace = InMemoryEvaluationNamespace()
        old, _, workspaces = executor(namespace)
        fresh, evaluation, replacement = executor(namespace)
        snapshot = await workspaces.root.snapshot("candidate")
        original = _request(snapshot)
        request = original.model_copy(
            update={
                "stages": tuple(
                    original.stages[i % 2].model_copy(update={"name": f"stage-{i}"})
                    for i in range(stage_count)
                )
            }
        )
        try:
            await old.submit(request, handle_id="local")
            completed = await yield_until(
                partial(old.poll, "local"),
                lambda p: p.phase is PollPhase.ENDED,
                "the completed local evaluation",
            )
            assert await fresh.recover("local") == completed
            assert completed.terminal is not None
            assert len(completed.terminal.stage_results) == stage_count
            await fresh.cancel("local")
            assert await fresh.recover("local") == completed
            assert not evaluation.accuracy_calls
            assert not replacement.candidates
        finally:
            await old.close()
            await fresh.close()

    run_virtual(VirtualClock(), case())


@pytest.mark.parametrize("running", [False, True])
async def test_confirmed_local_cancellation_survives_restart(*, running: bool) -> None:
    namespace = InMemoryEvaluationNamespace()
    old, evaluation, workspaces = executor(namespace)
    fresh, _, replacement = executor(namespace)
    snapshot = await workspaces.root.snapshot("candidate")
    gate = evaluation.gate("accuracy", 0) if running else None
    try:
        await old.submit(_request(snapshot), handle_id="local")
        if gate is not None:
            await yield_until(
                partial(old.poll, "local"),
                lambda p: p.phase is PollPhase.RUNNING,
                "the gated accuracy stage",
            )
            assert gate.entered.is_set()
        await old.cancel("local")
        recovered = await fresh.recover("local")
        assert recovered.phase is PollPhase.ENDED
        assert recovered.terminal is not None
        assert recovered.terminal.state is EvaluationState.CANCELED
        assert not replacement.candidates
    finally:
        await old.close()
        await fresh.close()


@example(completed=2**54, required=2**55)
@given(
    completed=st.integers(min_value=0, max_value=2**60),
    required=st.integers(min_value=1, max_value=2**60),
)
def test_partial_measurement_and_exact_progress_survive_local_receipts(
    completed: int, required: int
) -> None:
    async def case() -> None:
        namespace = InMemoryEvaluationNamespace()
        old, evaluation, workspaces = executor(namespace)
        fresh, _, replacement = executor(namespace)
        snapshot = await workspaces.root.snapshot("candidate")
        evaluation.default_benchmark = BenchmarkEvaluation(
            executed=True,
            feedback="interrupted",
            operator_feedback="interrupted",
            failure_kind=BenchmarkFailureKind.INFRASTRUCTURE,
            partial_measurement=PartialMeasurement(
                name="throughput",
                value=4.5,
                direction="max",
                unit="rps",
                target=9,
                progress=Progress(completed=completed, required=required, unit="requests"),
            ),
        )
        try:
            await old.submit(_request(snapshot), handle_id="local")
            observed = await yield_until(
                partial(old.poll, "local"),
                lambda p: p.phase is PollPhase.ENDED,
                "the stopped benchmark's retained partial measurement",
            )
            assert observed.terminal is not None
            assert observed.terminal.state is EvaluationState.FAILED
            assert await fresh.recover("local") == observed
            step = observed.terminal.stage_results[-1]
            evidence = TrustedEvidence.model_validate(step.result)
            assert evidence.partial_measurement == evaluation.default_benchmark.partial_measurement
            assert not replacement.candidates
        finally:
            await old.close()
            await fresh.close()

    run_virtual(VirtualClock(), case())


@pytest.mark.regression
@example(late_state=EvaluationState.CANCELED)
@given(late_state=st.sampled_from(list(EvaluationState)))
def test_late_cleanup_observations_cannot_replace_retained_terminal_results(
    late_state: EvaluationState,
) -> None:
    async def case() -> None:
        namespace = InMemoryEvaluationNamespace()
        old, _, workspaces = executor(namespace)
        fresh, _, _ = executor(namespace)
        snapshot = await workspaces.root.snapshot("candidate")
        try:
            await old.submit(_request(snapshot), handle_id="local")
            retained = await yield_until(
                partial(old.poll, "local"),
                lambda p: p.phase is PollPhase.ENDED,
                "the retained terminal observation",
            )
            LocalEvaluationReceipts(namespace).observe(
                "local",
                ExecutorObservation(
                    state=late_state,
                    failure="late failure" if late_state is EvaluationState.FAILED else None,
                ),
            )
            assert await fresh.recover("local") == retained
        finally:
            await old.close()
            await fresh.close()

    run_virtual(VirtualClock(), case())


@pytest.mark.regression
async def test_missing_local_receipt_reports_a_prerequisite_instead_of_unknown() -> None:
    fresh, evaluation, workspaces = executor(InMemoryEvaluationNamespace())
    with pytest.raises(EvaluationRecoveryBlockedError, match="Recovery blocked"):
        await fresh.recover("local")
    assert not evaluation.accuracy_calls
    assert not workspaces.candidates
    await fresh.close()


async def assert_local_path_recovery(
    fresh: PollingEvaluationExecutor,
    receipt: evaluation_receipts.LocalEvaluationReceipt | None,
    path: Literal["success", "failure", "cancel"],
) -> None:
    for _ in range(2):
        if receipt is None:
            with pytest.raises(EvaluationRecoveryBlockedError):
                await fresh.recover("local")
            with pytest.raises(ExecutorCancellationUnknownError):
                await fresh.cancel("local")
            with pytest.raises(EvaluationRecoveryBlockedError):
                await fresh.wait_for_change("local", 1)
        elif not receipt.cleaned:
            await assert_blocked(fresh)
        else:
            poll = await fresh.recover("local")
            assert poll.phase is PollPhase.ENDED
            assert poll.terminal is not None
            expected = {
                "success": EvaluationState.SUCCEEDED,
                "failure": EvaluationState.FAILED,
                "cancel": EvaluationState.CANCELED,
            }[path]
            assert poll.terminal.state is expected
            assert receipt.observation is not None
            assert len(poll.terminal.stage_results) == len(receipt.observation.stage_results)


async def local_crash_path(
    path: Literal["success", "failure", "cancel"], crash: int | None, *, before: bool
) -> int:
    namespace = ReceiptWrites(crash, before=before)
    old, evaluation, workspaces = executor(namespace)
    fresh, replacement_evaluation, replacement = executor(namespace)
    if path == "failure":
        evaluation.accuracy_results.append(RuntimeError("stage failed"))
    gate = evaluation.gate("accuracy", 0) if path == "cancel" else None
    snapshot = await workspaces.root.snapshot("candidate")

    async def stopped_or_at_stage() -> bool:
        if namespace.crashed:
            return True
        poll = await old.poll("local")
        return poll.phase is PollPhase.ENDED or (gate is not None and gate.entered.is_set())

    try:
        try:
            await old.submit(_request(snapshot), handle_id="local")
        except EvaluationReceiptUnconfirmedError:
            assert namespace.crashed
        await yield_until(stopped_or_at_stage, bool, "the local path's durable boundary")
        if gate is not None and not namespace.crashed:
            try:
                await old.cancel("local")
            except EvaluationReceiptUnconfirmedError:
                assert namespace.crashed
        writes = len(namespace.gate.calls)
        if crash is not None:
            assert namespace.crashed
        namespace.gate.heal()
        receipt = LocalEvaluationReceipts(namespace).load("local")
        await assert_local_path_recovery(fresh, receipt, path)
        assert not replacement.candidates
        assert not replacement_evaluation.accuracy_calls
        assert not replacement_evaluation.benchmark_calls
        return writes
    finally:
        await old.close()
        await fresh.close()


@pytest.mark.regression
@example(path="success", ordinal=1, before=True)
@example(path="failure", ordinal=10, before=False)
@example(path="cancel", ordinal=10, before=True)
@given(
    path=st.sampled_from(["success", "failure", "cancel"]),
    ordinal=st.integers(min_value=1, max_value=14),
    before=st.booleans(),
)
def test_local_receipt_faults_cover_before_write_and_all_terminal_paths(
    path: Literal["success", "failure", "cancel"], ordinal: int, *, before: bool
) -> None:
    writes = run_virtual(VirtualClock(), local_crash_path(path, None, before=before))
    run_virtual(VirtualClock(), local_crash_path(path, min(ordinal, writes), before=before))
