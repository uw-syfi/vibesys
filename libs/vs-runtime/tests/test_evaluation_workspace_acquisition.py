"""Every path that acquires inherited evaluation backing requires writer verification."""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api.testing import InMemoryEvaluationNamespace, wait_until_executor_started
from vs_project.api import Project, StoreFence
from vs_proto.api import replay
from vs_runtime.api import replay as replay_api
from vs_runtime.api.slurm_evaluation_stack import ScenarioCluster, build_stack
from vs_sim.api.testing import SimThreads
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
    backing = replay.WriterBacking(
        environment="slurm", identity=str(tmp_path / "workspace"), generation=1, authority="host-a"
    )
    registry = replay_api.WriterRegistry(
        Project.open(tmp_path).state.state_store_namespace("writers"), backing, SimThreads()
    )
    registry.create()
    registry.grant(
        replay.WriterIdentity(
            run_id="run", host_incarnation="host-a/1", physical_id=_HANDLE, domain_id="job-a"
        )
    )
    manifest = registry.close()
    proof = replay.WriterTerminationProof(manifest=manifest, absent_domains=["job-a"])
    writers = replay_api.EvaluationWriterReconciliation(
        registry,
        replay_api.ReplayRootReuse(proof.to_binary()),
        "run",
        StoreFence(host_id="host-a", epoch=1, expires_at=100),
    )
    old = await build_stack(tmp_path, cluster, namespace=namespace, replay_recovery=True)
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
        cluster.finish_jobs()
        registry.record(
            replay.WriterReconciliation(
                manifest=manifest,
                proof=proof,
                status=replay.WriterReconciliationStatus.PROVEN_STOPPED,
            )
        )
        await fresh.executor.submit(request, handle_id=_HANDLE)
        assert fresh.workspaces.candidates
        assert cluster.submissions == [_HANDLE]
    finally:
        await fresh.executor.close()
        await old.executor.close()


@pytest.mark.regression
@pytest.mark.parametrize("race", ["writer", "generation"])
def test_evaluation_acquisition_holds_the_recorded_proof_through_workspace_creation(
    tmp_path: Path, race: str
) -> None:
    namespace = Project.open(tmp_path).state.state_store_namespace("writers")
    backing = replay.WriterBacking(
        environment="local", identity="root-a", generation=1, authority="host-a"
    )
    threads = SimThreads()
    registry = replay_api.WriterRegistry(namespace, backing, threads)
    registry.create()
    manifest = registry.close()
    proof = replay.WriterTerminationProof(manifest=manifest)
    registry.record(
        replay.WriterReconciliation(
            manifest=manifest, proof=proof, status=replay.WriterReconciliationStatus.PROVEN_STOPPED
        )
    )
    request = replay_api.ReplayRootReuse(proof.to_binary())
    binding = replay_api.EvaluationWriterReconciliation(
        registry, request, "run", StoreFence(host_id="host-a", epoch=1, expires_at=100)
    )
    acquired = replay_api.EvaluationWorkspaceAcquisitions(
        InMemoryEvaluationNamespace(), "local", binding
    )
    competitor = replay_api.WriterRegistry(namespace, backing, threads)
    successor = deepcopy(backing)
    successor.generation += 1
    next_generation = replay_api.WriterRegistry(namespace, successor, threads)
    if race == "generation":
        next_generation.create()
        with (
            pytest.raises(replay_api.EvaluationRecoveryBlockedError, match="generation"),
            acquired.acquire("evaluation", "root-a", fresh=True, live=()),
        ):
            pytest.fail("stale generation reached workspace creation")
    else:
        with acquired.acquire("evaluation", "root-a", fresh=True, live=()):
            with (
                pytest.raises(replay_api.WriterProofError, match="another writer"),
                competitor.acquire(
                    request,
                    run_id="other",
                    fence=StoreFence(host_id="host-b", epoch=2, expires_at=100),
                ),
            ):
                pytest.fail("a second writer entered during workspace creation")
            with pytest.raises(replay_api.WriterProofError, match="another writer"):
                next_generation.create()
        next_generation.create()


@pytest.mark.regression
@pytest.mark.parametrize("first_out", ["a", "b"])
def test_overlapping_acquisitions_hold_exclusion_until_the_last_one_exits(
    tmp_path: Path, first_out: str
) -> None:
    """Concurrent evaluation tasks exit in any order; none may release another's exclusion."""
    namespace = Project.open(tmp_path).state.state_store_namespace("writers")
    backing = replay.WriterBacking(
        environment="local", identity="root-a", generation=1, authority="host-a"
    )
    threads = SimThreads()
    registry = replay_api.WriterRegistry(namespace, backing, threads)
    registry.create()
    manifest = registry.close()
    proof = replay.WriterTerminationProof(manifest=manifest)
    registry.record(
        replay.WriterReconciliation(
            manifest=manifest, proof=proof, status=replay.WriterReconciliationStatus.PROVEN_STOPPED
        )
    )
    request = replay_api.ReplayRootReuse(proof.to_binary())
    binding = replay_api.EvaluationWriterReconciliation(
        registry, request, "run", StoreFence(host_id="host-a", epoch=1, expires_at=100)
    )
    acquired = replay_api.EvaluationWorkspaceAcquisitions(
        InMemoryEvaluationNamespace(), "local", binding
    )
    competitor = replay_api.WriterRegistry(namespace, backing, threads)
    other = StoreFence(host_id="host-b", epoch=2, expires_at=100)

    def blocked() -> None:
        with (
            pytest.raises(replay_api.WriterProofError, match="another writer"),
            competitor.acquire(request, run_id="other", fence=other),
        ):
            pytest.fail("a writer from another run entered while an acquisition was open")

    scopes = {
        "a": acquired.acquire("evaluation-a", "root-a", fresh=True, live=()),
        "b": acquired.acquire("evaluation-b", "root-a", fresh=True, live=("evaluation-a",)),
    }
    scopes["a"].__enter__()
    scopes["b"].__enter__()
    scopes[first_out].__exit__(None, None, None)
    blocked()
    scopes["b" if first_out == "a" else "a"].__exit__(None, None, None)
    with competitor.acquire(request, run_id="other", fence=other):
        pass
    with registry.acquire(request, run_id="run", fence=binding.fence):
        pass
