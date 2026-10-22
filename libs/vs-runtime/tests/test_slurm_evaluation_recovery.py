"""Fresh semantic owners inspect retained physical work without dispatching it again."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import EvaluationState, PollPhase
from vs_evaluation.api.testing import InMemoryEvaluationNamespace, wait_until_executor_started
from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_runtime.api.replay import EvaluationRecoveryBlockedError
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_slurm.api import SlurmJobStatus

from .test_evaluation_executor_contract import _request

if TYPE_CHECKING:
    from pathlib import Path

_HANDLE = "retained-evaluation"


@pytest.mark.regression
@pytest.mark.parametrize("ended", [False, True])
async def test_missing_semantic_receipt_cannot_hide_retained_paid_work(
    tmp_path: Path, *, ended: bool
) -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)
    old = await build_stack(tmp_path, cluster, namespace=InMemoryEvaluationNamespace())
    fresh = await build_stack(tmp_path, cluster, namespace=InMemoryEvaluationNamespace())
    try:
        await old.executor.submit(_request(old.snapshot), handle_id=_HANDLE)
        await wait_until_executor_started(cluster.accepted, old.executor, _HANDLE)
        if ended:
            cluster.finish_jobs()
        for _ in range(3):
            recovered = await fresh.executor.recover(_HANDLE)
            assert recovered.phase is PollPhase.UNKNOWN
            assert "semantic evaluation request" in recovered.detail
            assert cluster.submissions == [_HANDLE]
            assert not fresh.workspaces.candidates
    finally:
        await fresh.executor.close()
        await old.executor.close()


@pytest.mark.parametrize("ended", [False, True])
async def test_fresh_owner_retains_identity_and_evidence_across_a_recovery_crash(
    tmp_path: Path, *, ended: bool
) -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.PENDING,)
    old = await build_stack(tmp_path, cluster)
    gate = FaultGate(
        FaultPlan(
            seed=0,
            rules=(
                FaultRule(
                    boundary=Boundary.EXECUTOR_REQUEST,
                    target="recovery",
                    at=1,
                    fault=HostFault.CRASH_AFTER,
                ),
            ),
        )
    )
    try:
        await old.executor.submit(_request(old.snapshot), handle_id=_HANDLE)
        await wait_until_executor_started(cluster.accepted, old.executor, _HANDLE)
        if ended:
            cluster.finish_jobs()
        fresh = await build_stack(tmp_path, cluster)
        with pytest.raises(HostCrashError):
            await gate.around_async(
                Boundary.EXECUTOR_REQUEST, "recovery", partial(fresh.executor.recover, _HANDLE)
            )
        gate.heal()
        await fresh.executor.close()
        again = await build_stack(tmp_path, cluster)
        try:
            observed = await again.executor.recover(_HANDLE)
            assert observed.phase is (PollPhase.ENDED if ended else PollPhase.QUEUED)
            if ended:
                assert observed.terminal is not None
                assert observed.terminal.state is EvaluationState.SUCCEEDED
                assert len(observed.terminal.stage_results) == 2
                assert await again.executor.recover(_HANDLE) == observed
            assert cluster.submissions == [_HANDLE]
            assert not fresh.workspaces.candidates
            assert not again.workspaces.candidates
        finally:
            await again.executor.close()
    finally:
        await old.executor.close()


@pytest.mark.regression
async def test_new_slurm_handle_cannot_bypass_an_inherited_writer_grant(tmp_path: Path) -> None:
    namespace = InMemoryEvaluationNamespace()
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.RUNNING,)
    old = await build_stack(tmp_path, cluster, namespace=namespace, replay_recovery=True)
    fresh = await build_stack(tmp_path, cluster, namespace=namespace, replay_recovery=True)
    try:
        request = _request(old.snapshot)
        await old.executor.submit(request, handle_id=_HANDLE)
        await wait_until_executor_started(cluster.accepted, old.executor, _HANDLE)
        with pytest.raises(EvaluationRecoveryBlockedError) as blocked:
            await fresh.executor.submit(request, handle_id="replacement-evaluation")
        assert blocked.value.block.outstanding == [_HANDLE]
        assert not fresh.workspaces.candidates
        assert cluster.submissions == [_HANDLE]
    finally:
        await fresh.executor.close()
        await old.executor.close()
