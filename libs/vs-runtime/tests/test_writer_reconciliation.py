"""Backing-wide writer contracts: no release or fresh grant without verified death."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import TYPE_CHECKING, Literal

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from vs_agent.api import reap_recorded_agents
from vs_agent.api.testing import FakeConfinement
from vs_project.api import Project, StoreFence
from vs_proto.api import ValidationError, replay, validate
from vs_runtime.api.infrastructure import RunEnvironmentSpec, build_run_environment
from vs_runtime.api.replay import (
    DockerWriterReconciler,
    ReplayRootReuse,
    UnsupportedWriterReconciler,
    WriterProofError,
    WriterRegistry,
)
from vs_runtime.api.testing import idle_environment_system
from vs_sandbox.api.testing import FakeDockerEngine
from vs_sim.api import DISCARD
from vs_sim.api.testing import (
    FakePipedProcessLauncher,
    GatedBlockingRunner,
    SimThreads,
)

if TYPE_CHECKING:
    from pathlib import Path


BACKING = replay.WriterBacking(
    environment="docker", identity="root-a", generation=1, authority="fake-daemon"
)


def registry(tmp_path: Path) -> WriterRegistry:
    return WriterRegistry(
        Project.open(tmp_path).state.state_store_namespace("backing-a"), BACKING, SimThreads()
    )


def owner(engine: FakeDockerEngine) -> DockerWriterReconciler:
    def reap(containers: tuple[str, ...]) -> None:
        reap_recorded_agents(
            containers,
            docker=engine,
            log=lambda _: None,
            confinement_for=lambda _: FakeConfinement(),
        )

    return DockerWriterReconciler(engine, reap)


def test_docker_proof_covers_every_run_and_descendant_domain(tmp_path: Path) -> None:
    engine = FakeDockerEngine(launcher=FakePipedProcessLauncher())
    ledger = registry(tmp_path)
    ledger.create()
    containers = []
    for index in range(3):
        container = engine.run(
            ["docker", "run", "-d", "image"], timeout_seconds=1, output=DISCARD
        ).stdout.strip()
        containers.append(container)
        ledger.grant(
            replay.WriterIdentity(
                run_id=f"run-{index}",
                host_incarnation="host-a/1",
                physical_id=f"writer-{index}",
                domain_id=container,
            )
        )
    manifest = ledger.close()
    with pytest.raises(WriterProofError, match="Recovery blocked"):
        ledger.require_proof()
    result = owner(engine).reconcile_writers(manifest)
    assert result.status is replay.WriterReconciliationStatus.PROVEN_STOPPED
    ledger.record(result)
    assert ledger.require_proof().absent_domains == sorted(containers)
    assert engine.containers() == ()
    # Recovery itself is idempotent: absent domains need no new CLI session.
    assert owner(engine).reconcile_writers(manifest) == result
    assert registry(tmp_path).require_proof() == result.proof
    with pytest.raises(WriterProofError, match="closed"):
        ledger.grant(manifest.writers[0])


@pytest.mark.regression
@pytest.mark.parametrize("name", ["host", "modal", "slurm", "skypilot"])
def test_environment_noop_never_proves_inherited_writers_stopped(tmp_path: Path, name: str) -> None:
    manifest = replay.WriterManifest(
        backing=replay.WriterBacking(environment=name, identity="root", authority="host-a"),
        closed=True,
        writers=[
            replay.WriterIdentity(
                run_id="run-a",
                host_incarnation="host-a/1",
                physical_id="turn-a",
                domain_id="domain-a",
            )
        ],
    )
    config = tmp_path / "slurm.toml"
    config.write_text(
        '[slurm]\nname = "cluster"\nremote_workspace_root = "/shared/work"\n[slurm.transport]\nkind = "local"\n[vibesys]\n',
        encoding="utf-8",
    )
    environment = build_run_environment(
        RunEnvironmentSpec(name, {"config_path": str(config), "profile": "cluster"})
    )
    result = environment.reconcile_writers(
        manifest, system=idle_environment_system(), log=lambda _: None
    )
    assert result.status is replay.WriterReconciliationStatus.UNSUPPORTED
    assert result.proof is None
    assert result.manifest == manifest
    assert "evidence" in result.detail


@pytest.mark.regression
@example(closed=True, writers=["one", "one"])
@given(closed=st.booleans(), writers=st.lists(st.sampled_from(["one", "two"]), max_size=4))
def test_manifest_proof_requires_closed_complete_unique_evidence(
    *, closed: bool, writers: list[str]
) -> None:
    manifest = replay.WriterManifest(
        backing=BACKING,
        closed=closed,
        writers=[
            replay.WriterIdentity(
                run_id="run-a", host_incarnation="host-a/1", physical_id=w, domain_id=w
            )
            for w in writers
        ],
    )
    proof = replay.WriterTerminationProof(manifest=manifest, absent_domains=sorted(set(writers)))
    if closed and len(writers) == len(set(writers)):
        validate(proof)
    else:
        with pytest.raises(ValidationError):
            validate(proof)


def test_missing_manifest_or_other_generation_cannot_authorize_reuse(tmp_path: Path) -> None:
    ledger = registry(tmp_path)
    with pytest.raises(WriterProofError, match="absent"):
        ledger.close()
    ledger.create()
    manifest = ledger.close()
    forged = deepcopy(manifest)
    assert forged.backing is not None
    forged.backing.generation += 1
    with pytest.raises(WriterProofError, match="complete current"):
        ledger.record(
            replay.WriterReconciliation(
                manifest=forged,
                status=replay.WriterReconciliationStatus.PROVEN_STOPPED,
                proof=replay.WriterTerminationProof(manifest=forged),
            )
        )
    with pytest.raises(WriterProofError, match="Recovery blocked"):
        ledger.require_proof()


def test_wrong_daemon_never_proves_absence(tmp_path: Path) -> None:
    engine = FakeDockerEngine(launcher=FakePipedProcessLauncher())
    engine.daemon_id = "other-daemon"
    ledger = registry(tmp_path)
    ledger.create()
    result = owner(engine).reconcile_writers(ledger.close())
    assert result.status is replay.WriterReconciliationStatus.UNKNOWN
    assert result.proof is None
    assert "daemon" in result.detail
    ledger.record(result)
    with pytest.raises(WriterProofError, match="Recovery blocked"):
        ledger.require_proof()


@pytest.mark.regression
def test_removal_in_progress_retains_writers_until_verified_absent(tmp_path: Path) -> None:
    engine = FakeDockerEngine(launcher=FakePipedProcessLauncher())
    container = engine.run(
        ["docker", "run", "-d", "image"], timeout_seconds=1, output=DISCARD
    ).stdout.strip()
    engine.hold_removal(container)
    ledger = registry(tmp_path)
    ledger.create()
    ledger.grant(
        replay.WriterIdentity(
            run_id="run-a", host_incarnation="host-a/1", physical_id="turn-a", domain_id=container
        )
    )
    manifest = ledger.close()
    result = owner(engine).reconcile_writers(manifest)
    assert result.status is replay.WriterReconciliationStatus.PENDING
    ledger.record(result)
    with pytest.raises(WriterProofError, match="Recovery blocked"):
        ledger.require_proof()
    assert engine.containers()[0].running
    engine.finish_removal(container)
    stopped = owner(engine).reconcile_writers(manifest)
    assert stopped.status is replay.WriterReconciliationStatus.PROVEN_STOPPED
    ledger.record(stopped)
    assert ledger.require_proof() == stopped.proof


@pytest.mark.regression
@given(generation=st.integers(min_value=1, max_value=20))
@example(generation=2)
def test_unproven_generation_cannot_be_bypassed_by_recreating_registry(
    tmp_path_factory: pytest.TempPathFactory, generation: int
) -> None:
    project = Project.open(tmp_path_factory.mktemp("backing-generation"))
    namespace = project.state.state_store_namespace("backing")
    first = WriterRegistry(namespace, BACKING, SimThreads())
    first.create()
    first.grant(
        replay.WriterIdentity(
            run_id="older-run",
            host_incarnation="host-a/1",
            physical_id="old-child",
            domain_id="old-domain",
        )
    )
    first.close()
    replacement = deepcopy(BACKING)
    replacement.generation = generation
    other = WriterRegistry(namespace, replacement, SimThreads())
    if generation == BACKING.generation:
        other.create()  # The same closed head is idempotent, never fresh or empty.
    else:
        with pytest.raises(WriterProofError, match="predecessor"):
            other.create()
    with pytest.raises(WriterProofError, match="Recovery blocked"):
        other.require_proof()
    assert first.close().writers[0].physical_id == "old-child"


def test_proven_generation_carries_all_old_runs_and_fences_old_proofs(tmp_path: Path) -> None:
    namespace = Project.open(tmp_path).state.state_store_namespace("backing")
    first = WriterRegistry(namespace, BACKING, SimThreads())
    first.create()
    engine = FakeDockerEngine(launcher=FakePipedProcessLauncher())
    domain = engine.run(
        ["docker", "run", "-d", "image"], timeout_seconds=1, output=DISCARD
    ).stdout.strip()
    first.grant(
        replay.WriterIdentity(
            run_id="old-run", host_incarnation="host-a/1", physical_id="old-child", domain_id=domain
        )
    )
    first.record(owner(engine).reconcile_writers(first.close()))
    successor = deepcopy(BACKING)
    successor.generation += 1
    current = WriterRegistry(namespace, successor, SimThreads())
    current.create()
    with pytest.raises(WriterProofError, match="another active"):
        first.require_proof()
    with pytest.raises(WriterProofError, match="Recovery blocked"):
        current.require_proof()
    new_domain = engine.run(
        ["docker", "run", "-d", "image"], timeout_seconds=1, output=DISCARD
    ).stdout.strip()
    current.grant(
        replay.WriterIdentity(
            run_id="new-run",
            host_incarnation="host-b/2",
            physical_id="new-child",
            domain_id=new_domain,
        )
    )
    manifest = current.close()
    assert {writer.run_id for writer in manifest.writers} == {"old-run", "new-run"}
    current.record(owner(engine).reconcile_writers(manifest))
    assert current.require_proof().manifest == manifest
    forged = deepcopy(successor)
    forged.generation += 1
    forged.authority = "other-daemon"
    with pytest.raises(WriterProofError, match="same authority"):
        WriterRegistry(namespace, forged, SimThreads()).create()


def test_reuse_holds_nonblocking_cross_run_exclusion_until_effect_finishes(tmp_path: Path) -> None:
    namespace = Project.open(tmp_path).state.state_store_namespace("backing")
    ledger = WriterRegistry(namespace, BACKING, SimThreads())
    ledger.create()
    ledger.record(UnsupportedWriterReconciler("host").reconcile_writers(ledger.close()))
    other = WriterRegistry(namespace, BACKING, SimThreads())
    backing = deepcopy(BACKING)
    backing.generation += 1
    successor = WriterRegistry(namespace, backing, SimThreads())
    fence = StoreFence(host_id="host-a", epoch=1, expires_at=10)
    with ledger.reuse(run_id="run-a", fence=fence) as proof:
        assert proof == ledger.require_proof()
        with ledger.reuse(run_id="run-a", fence=fence):
            assert ledger.require_proof() == proof
        with pytest.raises(WriterProofError, match="held by another"):
            other.require_proof()
        with pytest.raises(WriterProofError, match="held by another"):
            successor.create()
        with (
            pytest.raises(WriterProofError, match="held by another"),
            ledger.reuse(run_id="run-b", fence=fence),
        ):
            pytest.fail("another run entered backing reuse")
    successor.create()
    with pytest.raises(WriterProofError, match="another active"):
        ledger.require_proof()


@pytest.mark.parametrize("ending", ["completed", "failed", "cancelled"])
async def test_reuse_request_holds_exclusion_across_awaited_acquisition(
    tmp_path: Path, ending: Literal["completed", "failed", "cancelled"]
) -> None:
    ledger = registry(tmp_path)
    ledger.create()
    ledger.record(UnsupportedWriterReconciler("host").reconcile_writers(ledger.close()))
    request = ReplayRootReuse(ledger.require_proof().to_binary())
    other = registry(tmp_path)
    fence = StoreFence(host_id="host-a", epoch=1, expires_at=10)
    gate = GatedBlockingRunner(held=True)

    async def acquire() -> None:
        with ledger.acquire(request, run_id="run-a", fence=fence) as authorized:
            assert authorized == request
            await gate.run(lambda: None)
            if ending == "failed":
                raise LookupError(ending)

    task = asyncio.create_task(acquire())
    try:
        await gate.wait_in_flight(1, task)
        with pytest.raises(WriterProofError, match="held by another"):
            other.require_proof()
        if ending == "cancelled":
            task.cancel()
        else:
            gate.release()
        if ending == "completed":
            await task
        else:
            with pytest.raises(asyncio.CancelledError if ending == "cancelled" else LookupError):
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert other.require_proof() == request.proof()
    wrong = deepcopy(request.proof())
    assert wrong.manifest is not None
    assert wrong.manifest.backing is not None
    wrong.manifest.backing.generation += 1
    with (
        pytest.raises(WriterProofError, match="differs from the active"),
        ledger.acquire(ReplayRootReuse(wrong.to_binary()), run_id="run-a", fence=fence),
    ):
        pytest.fail("another generation's proof authorized acquisition")
    assert other.require_proof() == request.proof()
