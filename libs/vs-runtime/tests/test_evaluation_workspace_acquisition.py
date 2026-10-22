"""Every path that acquires inherited evaluation backing requires writer verification."""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api.testing import InMemoryEvaluationNamespace, wait_until_executor_started
from vs_runtime.api import replay as replay_api
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_runtime.api.testing import FakeEvaluationWriterReconciliation
from vs_slurm.api import SlurmJobStatus

from .test_evaluation_executor_contract import _request

if TYPE_CHECKING:
    from pathlib import Path

_HANDLE = "retained-evaluation"


@pytest.mark.regression
@pytest.mark.parametrize("action", ["submit", "inspect", "wait", "cancel"])
@pytest.mark.parametrize("ended", [False, True])
async def test_inherited_slurm_acquisition_requires_writer_verification(
    tmp_path: Path, action: str, *, ended: bool
) -> None:
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.RUNNING,)
    old = await build_stack(tmp_path, cluster, replay_recovery=True)
    fresh = await build_stack(tmp_path, cluster, replay_recovery=True)
    try:
        request = _request(old.snapshot)
        await old.executor.submit(request, handle_id=_HANDLE)
        await wait_until_executor_started(cluster.accepted, old.executor, _HANDLE)
        if ended:
            cluster.finish_jobs()
        # Read-only recovery never acquires a workspace, even for running jobs.
        await fresh.executor.recover(_HANDLE)
        operations = {
            "submit": partial(fresh.executor.submit, request, handle_id=_HANDLE),
            "inspect": partial(fresh.executor.inspect, _HANDLE),
            "wait": partial(fresh.executor.wait_for_change, _HANDLE, 1),
            "cancel": partial(fresh.executor.cancel, _HANDLE),
        }
        if action == "submit":
            with pytest.raises(RuntimeError, match="Recovery blocked") as caught:
                await operations[action]()
            assert isinstance(caught.value, replay_api.EvaluationRecoveryBlockedError)
            assert caught.value.block.environment == "slurm"
            assert caught.value.block.backing == str(old.workspaces.root.path)
            assert caught.value.block.outstanding == [_HANDLE]
            assert "complete inherited writer set" in caught.value.block.prerequisite
        else:
            await operations[action]()
        assert not fresh.workspaces.candidates
        assert cluster.submissions == [_HANDLE]
        if action != "cancel":
            assert not cluster.cancelled
    finally:
        await fresh.executor.close()
        await old.executor.close()


async def test_the_environment_verifies_inherited_writers_before_any_acquisition(
    tmp_path: Path,
) -> None:
    namespace = InMemoryEvaluationNamespace()
    cluster = ScenarioCluster()
    cluster.states = (SlurmJobStatus.RUNNING,)
    writers = FakeEvaluationWriterReconciliation()
    old = await build_stack(
        tmp_path, cluster, namespace=namespace, writer_reconciliation=writers, replay_recovery=True
    )
    fresh = await build_stack(
        tmp_path, cluster, namespace=namespace, writer_reconciliation=writers, replay_recovery=True
    )
    try:
        request = _request(old.snapshot)
        await old.executor.submit(request, handle_id=_HANDLE)
        await wait_until_executor_started(cluster.accepted, old.executor, _HANDLE)
        with pytest.raises(replay_api.EvaluationRecoveryBlockedError):
            await fresh.executor.submit(request, handle_id=_HANDLE)
        assert not fresh.workspaces.candidates
        assert writers.requests[-1] == (str(old.workspaces.root.path), (_HANDLE,))
        cluster.finish_jobs()
        writers.confirm_stopped(str(old.workspaces.root.path), _HANDLE)
        await fresh.executor.submit(request, handle_id=_HANDLE)
        assert fresh.workspaces.candidates
        assert cluster.submissions == [_HANDLE]
    finally:
        await fresh.executor.close()
        await old.executor.close()
