"""A faulted cluster call gives the semantic Slurm executor a typed outcome, and a job is not lost.

The cluster fails as a real one does (a transport that is down, a command that errors, a garbled
or cut-off answer, an accounting gap, a job killed after it was accepted, one that waits in the
queue for good), at the n-th call of an operation. Whatever the faults, a submission, a read and
a cancellation either return or raise one of the executor's own errors; they never raise
anything else; and a job the cluster accepted is ended or has its cancellation acknowledged,
unless the cancellation itself was faulted.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_evaluation.api import (
    EvaluationState,
    ExecutorCancellationUnconfirmedError,
    ExecutorCancellationUnknownError,
    ExecutorRejectedError,
    ExecutorSubmissionError,
)
from vs_faults.api import ClusterFault, ClusterOperation
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_sandbox.api.slurm import SlurmOutcomeUnknownError
from vs_sim.api import yield_now
from vs_sim.api.testing import examples
from vs_slurm.api import ClusterUnknown, SlurmJobStatus

from .test_evaluation_executor_contract import _request

if TYPE_CHECKING:
    from vs_slurm.api import (
        ClusterCancelOutcome,
        ClusterCollectOutcome,
        ClusterInspectOutcome,
        ClusterObservation,
        ClusterSubmitOutcome,
        ClusterTarget,
        SlurmBatchRequest,
        SlurmJobRequest,
    )

_HANDLE = "faulted-evaluation"
_TYPED = (
    ExecutorSubmissionError,
    ExecutorCancellationUnknownError,
    ExecutorCancellationUnconfirmedError,
    ExecutorRejectedError,
    SlurmOutcomeUnknownError,
)
"""Everything the executor may raise: the failures it names for an owner to act on."""
_UNREADABLE = frozenset(
    {
        ClusterFault.SSH_DOWN,
        ClusterFault.COMMAND_ERROR,
        ClusterFault.WRONG_STATE,
        ClusterFault.ACCOUNTING_GAP,
        ClusterFault.TRUNCATED,
    }
)
"""Faults that leave a call's answer unknown, as the real cluster reports a failed or garbled one."""
_SUBMITTED_FATE = {
    ClusterFault.KILLED: (SlurmJobStatus.FAILED,),
    ClusterFault.PENDING_FOREVER: (SlurmJobStatus.PENDING,),
}
_CALLS = 6
_POLLS = 40


class FaultedCluster(ScenarioCluster):
    """The Fake cluster, with ``faults`` struck at the calls they name.

    Calls are counted per operation as the connector requests are: a submit is an ``sbatch``, a
    read an ``squeue`` and an ``sacct``, a cancel a ``scancel``, a collect a transfer and an
    exec. A fault fires on the ``at``-th call of its operation.
    """

    def __init__(self, faults: dict[tuple[ClusterOperation, int], ClusterFault]) -> None:
        super().__init__()
        self._faults = faults
        self._counts: Counter[ClusterOperation] = Counter()
        self.settled: set[str] = set()
        self.cancel_acknowledged: set[str] = set()
        self.cancel_faulted = False

    def _strike(self, *operations: ClusterOperation) -> ClusterFault | None:
        struck = None
        for operation in operations:
            self._counts[operation] += 1
            fault = self._faults.get((operation, self._counts[operation]))
            struck = struck or fault
        return struck

    def submit(
        self, request: SlurmJobRequest | SlurmBatchRequest, *, operation_id: str
    ) -> ClusterSubmitOutcome:
        fault = self._strike(ClusterOperation.SBATCH)
        if fault in {ClusterFault.SSH_DOWN, ClusterFault.COMMAND_ERROR}:
            return ClusterUnknown(operation_id=operation_id, reason=f"sbatch: {fault.value}")
        if fault is ClusterFault.TRUNCATED:
            super().submit(request, operation_id=operation_id)  # accepted; its id was cut off
            return ClusterUnknown(operation_id=operation_id, reason="sbatch: truncated")
        fate = _SUBMITTED_FATE.get(fault) if fault is not None else None
        if fate is None or operation_id in self.submissions:
            return super().submit(request, operation_id=operation_id)
        alive, self.states = self.states, fate
        try:
            return super().submit(request, operation_id=operation_id)
        finally:
            self.states = alive

    def inspect(self, target: ClusterTarget, *, by_job_id: bool = False) -> ClusterInspectOutcome:
        fault = self._strike(ClusterOperation.SQUEUE, ClusterOperation.SACCT)
        if fault in _UNREADABLE:
            return _unknown(target, by_job_id=by_job_id, reason=f"inspect: {fault.value}")
        outcome = super().inspect(target, by_job_id=by_job_id)
        if outcome.kind == "observed" and outcome.life.kind == "ended":
            self.settled.add(_operation(target, outcome))
        return outcome

    def cancel(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCancelOutcome:
        fault = self._strike(ClusterOperation.SCANCEL)
        self.cancel_faulted = self.cancel_faulted or fault is not None
        if fault in _UNREADABLE:
            return _unknown(target, by_job_id=by_job_id, reason=f"scancel: {fault.value}")
        outcome = super().cancel(target, by_job_id=by_job_id, observed=observed)
        if outcome.kind == "cancel_requested":
            self.cancel_acknowledged.add(_operation(target, outcome))
        return outcome

    def collect(
        self,
        target: ClusterTarget,
        *,
        by_job_id: bool = False,
        observed: ClusterObservation | None = None,
    ) -> ClusterCollectOutcome:
        fault = self._strike(ClusterOperation.TRANSFER, ClusterOperation.EXEC)
        if fault in _UNREADABLE:
            return _unknown(target, by_job_id=by_job_id, reason=f"collect: {fault.value}")
        return super().collect(target, by_job_id=by_job_id, observed=observed)


def _operation(target: object, outcome: object) -> str:
    """The operation identity a target, or the outcome it got, names."""
    named = getattr(outcome, "operation_id", None) or getattr(target, "invocation_id", None)
    return str(named if named is not None else target)


def _unknown(target: ClusterTarget, *, by_job_id: bool, reason: str) -> ClusterUnknown:
    return ClusterUnknown(
        operation_id=None if by_job_id else str(target),
        job_id=str(target) if by_job_id else None,
        reason=reason,
    )


_OPERATIONS = [
    ClusterOperation.SBATCH,
    ClusterOperation.SQUEUE,
    ClusterOperation.SACCT,
    ClusterOperation.SCANCEL,
    ClusterOperation.TRANSFER,
    ClusterOperation.EXEC,
]
_faults = st.dictionaries(
    st.tuples(st.sampled_from(_OPERATIONS), st.integers(1, _CALLS)),
    st.sampled_from(list(ClusterFault)),
    max_size=5,
)


@settings(
    max_examples=examples(pr=25, nightly=300),
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(faults=_faults, cancels=st.booleans())
async def test_a_faulted_cluster_gives_the_executor_a_typed_outcome_and_loses_no_job(
    faults: dict[tuple[ClusterOperation, int], ClusterFault], *, cancels: bool
) -> None:
    with tempfile.TemporaryDirectory() as root:
        await _play(Path(root), faults, cancels=cancels)


async def _play(
    root: Path, faults: dict[tuple[ClusterOperation, int], ClusterFault], *, cancels: bool
) -> None:
    cluster = FaultedCluster(faults)
    cluster.states = (SlurmJobStatus.PENDING, SlurmJobStatus.RUNNING, SlurmJobStatus.COMPLETED)
    stack = await build_stack(root, cluster)
    try:
        try:
            await stack.executor.submit(_request(stack.snapshot), handle_id=_HANDLE)
        except _TYPED:
            return
        ended = False
        for _ in range(_POLLS):
            try:
                observed = await stack.executor.inspect(_HANDLE)
            except _TYPED:
                break
            if observed is not None and observed.state in {
                EvaluationState.SUCCEEDED,
                EvaluationState.FAILED,
                EvaluationState.CANCELED,
            }:
                ended = True
                break
            await yield_now()
        if cancels or not ended:
            for _ in range(_CALLS):
                try:
                    await stack.executor.cancel(_HANDLE)
                    break
                except _TYPED:
                    continue
            await yield_now()
        for operation in cluster.submissions:
            if not cluster.cancel_faulted:
                assert operation in cluster.settled | cluster.cancel_acknowledged, (
                    faults,
                    operation,
                )
    finally:
        await stack.executor.close()
