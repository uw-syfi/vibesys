"""The recorder's persistence contract against memory, local CAS and native log."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Protocol

import pytest
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey
from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_project.api import (
    CommitFault,
    Committed,
    Conflict,
    FakeStateStore,
    Project,
    StateStore,
    StoredEnvelope,
)
from vs_proto.api import Ended, EpochStarted, Outcome, core_common, replay
from vs_proto.api import ReplayStore as Records
from vs_runlog.api import FileRunLogWriter, OsFileSync, open_run_log
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayCommitError,
    ReplayCompleted,
    ReplayEnding,
    ReplayLaunch,
    ReplayResult,
    ReplayRunHost,
    ReplayRuntime,
    ReplayStore,
    RunLogReplayStore,
    run_replay,
)
from vs_sim.api.testing import Sim, SimThreads, swept


class StoreFactory(Protocol):
    def __call__(
        self, fault_plan: tuple[CommitFault | None, ...] = ()
    ) -> AbstractContextManager[ReplayStore]: ...


@pytest.fixture(params=["fake", "local", "runlog"])
def stores(request: pytest.FixtureRequest, tmp_path: Path, sim: Sim) -> StoreFactory:
    ordinal = 0

    @contextmanager
    def opened(fault_plan: tuple[CommitFault | None, ...] = ()) -> Iterator[ReplayStore]:
        nonlocal ordinal
        root = tmp_path / f"case-{ordinal}"
        root.mkdir()
        project = Project.open(root)
        ordinal += 1
        if request.param == "fake":
            yield FakeStateStore(fault_plan=fault_plan)
        elif request.param == "local":
            yield project.state_store("run-a", fault_plan=fault_plan)
        else:
            path = project.run_log_path_for(project.root, "run-a")
            path.parent.mkdir(parents=True, exist_ok=True)
            threads = sim.threads()
            with FileRunLogWriter(path, clock=sim.clock, threads=threads) as writer:
                yield RunLogReplayStore(writer, project.state_store("run-a", fault_plan=fault_plan))

    return opened


def composed(store: ReplayStore, executor: FakeReplayExecutor, sim: Sim) -> ReplayRunHost:
    return ReplayRunHost(
        runtime(store),
        executor,
        sim.clock,
        lambda _: ReplayEnding(
            Ended(outcome=Outcome.FINISHED, summary="Measured.", resumable=True)
        ),
    )


async def test_native_fsync_crashes_reconcile_publication_without_repeating_paid_work(
    tmp_path: Path, sim: Sim
) -> None:
    async def once(
        project: Project, gate: FaultGate, executor: FakeReplayExecutor, request_id: str
    ) -> ReplayRunHost:
        path = project.run_log_path_for(project.root, "run-a")
        path.parent.mkdir(parents=True, exist_ok=True)
        real = OsFileSync()

        def sync(descriptor: int, /) -> None:
            gate.around(Boundary.DURABLE_WRITE, "fsync", partial(real, descriptor))

        threads = sim.threads()
        with FileRunLogWriter(path, clock=sim.clock, threads=threads, sync=sync) as writer:
            binding = RunLogReplayStore(writer, project.state_store("run-a"))
            host = composed(binding, executor, sim)
            assert await run_replay(host, launch(request_id)) == ReplayCompleted("measured")
            return host

    def executor() -> FakeReplayExecutor:
        return FakeReplayExecutor(
            lambda _: ReplayResult(core_common.Identity(root="measured"), paid=True, cost_usd=2)
        )

    baseline = tmp_path / "baseline"
    baseline.mkdir()
    gate = FaultGate(FaultPlan(seed=0))
    await once(Project.open(baseline), gate, executor(), "baseline")
    boundaries = len(gate.calls)
    for offset in swept(boundaries, sample=3):
        root = tmp_path / f"case-{offset}"
        root.mkdir()
        project = Project.open(root)
        gate = FaultGate(
            FaultPlan(
                seed=0,
                rules=(
                    FaultRule(
                        boundary=Boundary.DURABLE_WRITE,
                        target="fsync",
                        at=offset + 1,
                        fault=HostFault.CRASH_AFTER,
                    ),
                ),
            )
        )
        owner = executor()
        with suppress(HostCrashError):
            await once(project, gate, owner, "first")
        gate.heal()
        resumed = await once(project, gate, owner, "resume")
        assert len(owner.invocations) == 1
        assert resumed.runtime.accounting.lifetime_completed == 1
        assert resumed.runtime.accounting.reported_cost_usd == 2


@pytest.mark.parametrize("fault", list(CommitFault))
async def test_crash_sweep_keeps_one_paid_observation_across_every_store(
    stores: StoreFactory, sim: Sim, fault: CommitFault
) -> None:
    with stores() as baseline:
        executor = FakeReplayExecutor(
            lambda _: ReplayResult(core_common.Identity(root="measured"), paid=True, cost_usd=2)
        )
        original = composed(baseline, executor, sim)
        await run_replay(original, launch("baseline"))
        boundaries = original.runtime.archive.sequence
    for boundary in swept(boundaries, sample=3):
        with stores((None,) * boundary + (fault,)) as store:
            executor = FakeReplayExecutor(
                lambda _: ReplayResult(core_common.Identity(root="measured"), paid=True, cost_usd=2)
            )
            with suppress(ReplayCommitError, OSError):
                await run_replay(composed(store, executor, sim), launch(f"first-{boundary}"))
            resumed = composed(store, executor, sim)
            assert await run_replay(resumed, launch(f"resume-{boundary}")) == ReplayCompleted(
                "measured"
            )
            assert len(executor.invocations) == 1
            assert resumed.runtime.accounting.lifetime_completed == 1
            assert resumed.runtime.accounting.paid_operations == 1
            assert resumed.runtime.accounting.reported_cost_usd == 2


def launch(request: str) -> ReplayLaunch:
    return ReplayLaunch(EpochStarted(run_id="run-a"), request, "host-a", 30)


async def root(ctx: ReplayFlow) -> str:
    async with ctx.iteration("round", 1):
        result = await ctx.call(
            ReplayCall(
                SemanticKey("measure"),
                core_common.Identity(root="candidate"),
                "test.v1",
                replay.ReplayPolicy(reply_type=core_common.Identity.desc().type_name),
                core_common.Identity,
            )
        )
    return result.root


def runtime(store: ReplayStore) -> ReplayRuntime:
    return ReplayRuntime(store, root, replay.ReplayAllowance(iterations=2))


@pytest.mark.parametrize("kind", ["fake", "local", "runlog"])
def test_retained_formats_have_the_same_import_prerequisite(
    kind: str, tmp_path: Path, sim: Sim
) -> None:
    project = Project.open(tmp_path)
    authority: StateStore = FakeStateStore() if kind == "fake" else project.state_store("run-a")
    fence = authority.acquire("host-a", 0, 30)
    assert fence is not None
    old = StoredEnvelope(revision=0, schema_version=1, payload=b"retained-format")
    assert isinstance(authority.commit(None, old, fence, 0), Committed)
    assert authority.release(fence, 0)
    with ExitStack() as resources:
        store: ReplayStore = authority
        if kind == "runlog":
            path = project.run_log_path_for(tmp_path, "run-a")
            path.parent.mkdir(parents=True, exist_ok=True)
            threads = sim.threads()
            writer = resources.enter_context(
                FileRunLogWriter(path, clock=sim.clock, threads=threads)
            )
            store = RunLogReplayStore(writer, authority)
        with pytest.raises(ReplayCommitError, match="facts-only import"):
            runtime(store).start(launch("resume"), now=0, at=Timestamp())
        assert authority.load() == old


async def test_confirmed_prefix_replays_without_reexecuting(stores: StoreFactory, sim: Sim) -> None:
    with stores() as store:
        executor = FakeReplayExecutor(
            lambda _: ReplayResult(core_common.Identity(root="measured"), paid=True, cost_usd=2)
        )

        def host() -> ReplayRunHost:
            return ReplayRunHost(
                runtime(store),
                executor,
                sim.clock,
                lambda _: ReplayEnding(
                    Ended(outcome=Outcome.FINISHED, summary="Measured.", resumable=True)
                ),
            )

        assert await run_replay(host(), launch("first")) == ReplayCompleted("measured")
        first = store.load()
        assert isinstance(first, StoredEnvelope)
        assert await run_replay(host(), launch("resume")) == ReplayCompleted("measured")
        assert len(executor.invocations) == 1
        resumed = store.load()
        assert isinstance(resumed, StoredEnvelope)
        assert resumed.revision > first.revision
        assert resumed.payload.startswith(first.payload)


def test_stale_writer_cannot_commit_or_dispatch(stores: StoreFactory) -> None:
    with stores() as store:
        first = runtime(store)
        first.start(launch("first"), now=0, at=Timestamp())
        previous = store.load()
        assert isinstance(previous, StoredEnvelope)
        first.close(now=0)
        fence = store.acquire("host-b", 0, 30)
        assert fence is not None
        stale = store.acquire("host-a", 0, 30)
        assert stale is None
        stored = store.load()
        assert stored == previous
        assert store.release(fence, 0)


def test_revision_conflict_publishes_nothing(stores: StoreFactory) -> None:
    with stores() as store:
        shell = runtime(store)
        shell.start(launch("first"), now=0, at=Timestamp())
        shell.close(now=0)
        stored = store.load()
        assert isinstance(stored, StoredEnvelope)
        fence = store.acquire("host-b", 0, 30)
        assert fence is not None
        outcome = store.commit(None, stored, fence, 0)
        assert isinstance(outcome, Conflict)
        assert store.load() == stored
        assert store.release(fence, 0)


def test_native_log_is_rebuilt_from_the_authoritative_atomic_store(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    path = project.run_log_path_for(tmp_path, "run-a")
    path.parent.mkdir(parents=True, exist_ok=True)
    leases = project.state_store("run-a")
    threads = SimThreads()
    with FileRunLogWriter(path, clock=threads.clock, threads=threads) as writer:
        shell = runtime(RunLogReplayStore(writer, leases))
        shell.start(launch("first"), now=0, at=Timestamp())
        shell.prepare(now=0, at=Timestamp())
        expected = shell.archive.encode()
        shell.close(now=0)
    committed = leases.load()
    assert isinstance(committed, StoredEnvelope)
    assert committed.payload == expected
    with open_run_log(path) as reader:
        assert Records(records=list(reader)).to_binary() == expected
    path.with_name("run.head").unlink(missing_ok=True)
    keyframes = path.parent / "keyframes"
    retained = list(keyframes.glob("*.pb"))
    assert retained
    for keyframe in retained:
        keyframe.unlink()
    keyframes.rmdir()
    path.unlink()
    threads = SimThreads()
    with FileRunLogWriter(path, clock=threads.clock, threads=threads) as writer:
        stored = RunLogReplayStore(writer, leases).load()
        assert isinstance(stored, StoredEnvelope)
        assert stored.payload == expected
