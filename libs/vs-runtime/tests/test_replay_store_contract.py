"""The recorder's persistence contract against memory, local CAS and native log."""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_core.api.replay import ReplayCall, ReplayFlow, SemanticKey
from vs_project.api import Conflict, FakeStateStore, Project, StoredEnvelope
from vs_proto.api import Ended, EpochStarted, Outcome, core_common, replay
from vs_proto.api import ReplayStore as Records
from vs_runlog.api import FileRunLogWriter, open_run_log
from vs_runtime.api.replay import (
    FakeReplayExecutor,
    ReplayCompleted,
    ReplayEnding,
    ReplayLaunch,
    ReplayResult,
    ReplayRunHost,
    ReplayRuntime,
    ReplayStore,
    RunLogReplayStore,
    drive_replay,
)
from vs_sim.api.testing import Sim, SimThreads

StoreFactory = Callable[[], AbstractContextManager[ReplayStore]]


@pytest.fixture(params=["fake", "local", "runlog"])
def stores(request: pytest.FixtureRequest, tmp_path: Path, sim: Sim) -> StoreFactory:
    @contextmanager
    def opened() -> Iterator[ReplayStore]:
        project = Project.open(tmp_path)
        if request.param == "fake":
            yield FakeStateStore()
        elif request.param == "local":
            yield project.state_store("run-a")
        else:
            path = project.run_log_path_for(tmp_path, "run-a")
            path.parent.mkdir(parents=True, exist_ok=True)
            threads = sim.threads()
            with FileRunLogWriter(path, clock=sim.clock, threads=threads) as writer:
                yield RunLogReplayStore(writer, project.state_store("run-a"))

    return opened


def launch(request: str) -> ReplayLaunch:
    return ReplayLaunch(EpochStarted(run_id="run-a"), request, "host-a", 30)


async def root(ctx: ReplayFlow) -> str:
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

        assert await drive_replay(host(), launch("first")) == ReplayCompleted("measured")
        first = store.load()
        assert isinstance(first, StoredEnvelope)
        assert await drive_replay(host(), launch("resume")) == ReplayCompleted("measured")
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


def test_native_log_is_authority_and_lease_store_has_no_envelope(tmp_path: Path) -> None:
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
    assert leases.load() is None
    with open_run_log(path) as reader:
        assert Records(records=list(reader)).to_binary() == expected
    path.with_name("run.head").unlink(missing_ok=True)
    path.with_name("run.keyframe").unlink(missing_ok=True)
    threads = SimThreads()
    with FileRunLogWriter(path, clock=threads.clock, threads=threads) as writer:
        stored = RunLogReplayStore(writer, leases).load()
        assert isinstance(stored, StoredEnvelope)
        assert stored.payload == expected
