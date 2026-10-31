"""The run.log replay store's persistence contract against memory, local CAS and native log."""

from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from functools import partial
from pathlib import Path
from typing import Protocol

import pytest
from protobuf import Oneof
from protobuf.wkt.google.protobuf.timestamp_pb import Timestamp

from vs_faults.api import Boundary, FaultGate, FaultPlan, FaultRule, HostCrashError, HostFault
from vs_project.api import (
    CommitFault,
    Committed,
    Conflict,
    ConflictReason,
    FakeStateStore,
    Project,
    StoredEnvelope,
    StoreFence,
)
from vs_proto.api import EpochStarted, Lifecycle, Parked, Record
from vs_proto.api import ReplayStore as Records
from vs_runlog.api import FileRunLogWriter, FileSync, OsFileSync, open_run_log
from vs_runtime.api.replay import REPLAY_STORE_SCHEMA_VERSION, ReplayStore, RunLogReplayStore
from vs_sim.api.testing import Sim, SimThreads, swept

RUN = "run-a"
LENGTH = 4


class StoreFactory(Protocol):
    def __call__(
        self, fault_plan: tuple[CommitFault | None, ...] = ()
    ) -> AbstractContextManager[ReplayStore]: ...


def chain(count: int) -> tuple[Record, ...]:
    """A dense run.log prefix: the epoch start, then parked lifecycle records."""
    return tuple(
        Record(
            seq=seq,
            epoch=1,
            clock=0.0,
            at=Timestamp(),
            body=Oneof("epoch_started", EpochStarted(run_id=RUN, epoch=1))
            if seq == 1
            else Oneof("lifecycle", Lifecycle(state=Oneof("parked", Parked()))),
        )
        for seq in range(1, count + 1)
    )


def envelope(count: int) -> StoredEnvelope:
    return StoredEnvelope(
        revision=count - 1,
        schema_version=REPLAY_STORE_SCHEMA_VERSION,
        payload=Records(records=list(chain(count))).to_binary(),
    )


def committed(store: ReplayStore) -> int:
    stored = store.load()
    if stored is None:
        return 0
    assert isinstance(stored, StoredEnvelope)
    return stored.revision + 1


def extend(store: ReplayStore, fence: StoreFence, upto: int = LENGTH, now: float = 0) -> bool:
    """Commit the missing records one at a time; false at the first unconfirmed commit."""
    for count in range(committed(store) + 1, upto + 1):
        revision = None if count == 1 else count - 2
        if not isinstance(store.commit(revision, envelope(count), fence, now), Committed):
            return False
    return True


def log_writer(
    project: Project, sim: Sim, sync: FileSync | None = None, threads: SimThreads | None = None
) -> FileRunLogWriter:
    path = project.run_log_path_for(project.root, RUN)
    path.parent.mkdir(parents=True, exist_ok=True)
    return FileRunLogWriter(path, clock=sim.clock, threads=threads or sim.threads(), sync=sync)


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
            yield project.state_store(RUN, fault_plan=fault_plan)
        else:
            with log_writer(project, sim) as writer:
                yield RunLogReplayStore(
                    writer, project.state_store(RUN, fault_plan=fault_plan), SimThreads()
                )

    return opened


@pytest.mark.parametrize("count", range(1, LENGTH + 1))
def test_every_store_loads_exactly_the_records_committed(stores: StoreFactory, count: int) -> None:
    with stores() as store:
        fence = store.acquire("host-a", 0, 30)
        assert fence is not None
        assert extend(store, fence, count)
        assert store.load() == envelope(count)
        assert store.release(fence, 0)


def test_stale_writer_cannot_commit(stores: StoreFactory) -> None:
    with stores() as store:
        first = store.acquire("host-a", 0, 30)
        assert first is not None
        assert extend(store, first, 2)
        assert store.release(first, 0)
        second = store.acquire("host-b", 0, 30)
        assert second is not None
        outcome = store.commit(1, envelope(3), first, 0)
        assert isinstance(outcome, Conflict)
        assert outcome.reason is ConflictReason.FENCE
        assert store.load() == envelope(2)
        assert store.release(second, 0)


def test_revision_conflict_publishes_nothing(stores: StoreFactory) -> None:
    with stores() as store:
        fence = store.acquire("host-a", 0, 30)
        assert fence is not None
        assert extend(store, fence, 2)
        outcome = store.commit(None, envelope(1), fence, 0)
        assert isinstance(outcome, Conflict)
        assert outcome.reason is ConflictReason.REVISION
        assert store.load() == envelope(2)
        assert store.release(fence, 0)


@pytest.mark.parametrize("fault", list(CommitFault))
def test_a_faulted_commit_never_loses_or_duplicates_a_record(
    stores: StoreFactory, fault: CommitFault
) -> None:
    for boundary in range(LENGTH):
        with stores((None,) * boundary + (fault,)) as store:
            fence = store.acquire("host-a", 0, 30)
            assert fence is not None
            with suppress(OSError):
                extend(store, fence)
            store.release(fence, 0)
            resumed = store.acquire("host-a", 0, 30)
            assert resumed is not None
            assert extend(store, resumed)
            assert store.load() == envelope(LENGTH)


@pytest.mark.parametrize("fault", list(HostFault))
def test_a_fault_at_any_fsync_resumes_to_the_same_log(
    tmp_path: Path, sim: Sim, fault: HostFault
) -> None:
    def settle(project: Project, gate: FaultGate, now: float) -> None:
        real = OsFileSync()

        def sync(descriptor: int, /) -> None:
            gate.around(Boundary.DURABLE_WRITE, "fsync", partial(real, descriptor))

        with log_writer(project, sim, sync) as writer:
            store = RunLogReplayStore(writer, project.state_store(RUN), SimThreads())
            fence = store.acquire("host-a", now, 30)
            assert fence is not None
            extend(store, fence, now=now)

    baseline = FaultGate(FaultPlan(seed=0))
    (tmp_path / "baseline").mkdir()
    settle(Project.open(tmp_path / "baseline"), baseline, 0)
    for offset in swept(len(baseline.calls), sample=4):
        root = tmp_path / f"case-{offset}"
        root.mkdir()
        project = Project.open(root)
        rule = FaultRule(
            boundary=Boundary.DURABLE_WRITE, target="fsync", at=offset + 1, fault=fault
        )
        gate = FaultGate(FaultPlan(seed=0, rules=(rule,)))
        with suppress(HostCrashError, OSError):
            settle(project, gate, 0)
        gate.heal()
        settle(project, gate, 100)  # The crashed process's lease has lapsed.
        with log_writer(project, sim) as writer:
            assert RunLogReplayStore(
                writer, project.state_store(RUN), SimThreads()
            ).load() == envelope(LENGTH)


def written_run(tmp_path: Path, sim: Sim) -> tuple[Project, Path]:
    project = Project.open(tmp_path)
    with log_writer(project, sim) as writer:
        store = RunLogReplayStore(writer, project.state_store(RUN), SimThreads())
        fence = store.acquire("host-a", 0, 30)
        assert fence is not None
        assert extend(store, fence)
        assert store.release(fence, 0)
    return project, project.run_log_path_for(tmp_path, RUN)


def reload(project: Project) -> StoredEnvelope | None:
    threads = SimThreads()
    path = project.run_log_path_for(project.root, RUN)
    with FileRunLogWriter(path, clock=threads.clock, threads=threads) as writer:
        stored = RunLogReplayStore(writer, project.state_store(RUN), SimThreads()).load()
    assert stored is None or isinstance(stored, StoredEnvelope)
    return stored


def test_run_log_alone_holds_the_records_and_survives_deleted_caches(
    tmp_path: Path, sim: Sim
) -> None:
    project, path = written_run(tmp_path, sim)
    assert project.state_store(RUN).load() == StoredEnvelope(
        revision=0, schema_version=REPLAY_STORE_SCHEMA_VERSION, payload=b""
    )
    with open_run_log(path) as reader:
        assert Records(records=list(reader)).to_binary() == envelope(LENGTH).payload
    path.with_name("run.head").unlink(missing_ok=True)
    keyframes = path.parent / "keyframes"
    for keyframe in keyframes.glob("*.pb"):
        keyframe.unlink()
    assert reload(project) == envelope(LENGTH)


def test_a_corrupt_log_fails_and_the_atomic_store_cannot_repair_it(
    tmp_path: Path, sim: Sim
) -> None:
    project, path = written_run(tmp_path, sim)
    data = bytearray(path.read_bytes())
    data[12] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(ValueError, match=r"corrupt run\.log"):
        reload(project)
    assert project.state_store(RUN).load() == StoredEnvelope(
        revision=0, schema_version=REPLAY_STORE_SCHEMA_VERSION, payload=b""
    )


def test_a_torn_final_frame_recovers_the_complete_record_prefix(tmp_path: Path, sim: Sim) -> None:
    project, path = written_run(tmp_path, sim)
    with path.open("ab") as stream:
        stream.write(b"\x05\x00")
    assert reload(project) == envelope(LENGTH)


def test_records_without_the_format_fence_grant_no_authority(tmp_path: Path, sim: Sim) -> None:
    written = tmp_path / "written"
    written.mkdir()
    _, path = written_run(written, sim)
    unfenced = Project.open(tmp_path)
    copied = unfenced.run_log_path_for(tmp_path, RUN)
    copied.parent.mkdir(parents=True, exist_ok=True)
    copied.write_bytes(path.read_bytes())
    with pytest.raises(ValueError, match="format fence"):
        reload(unfenced)


def test_a_retained_positional_store_requires_facts_only_import(tmp_path: Path, sim: Sim) -> None:
    project = Project.open(tmp_path)
    store = project.state_store(RUN)
    retained = StoredEnvelope(revision=0, schema_version=1, payload=b"positional")
    owner = store.acquire("host-a", 0, 30)
    assert owner is not None
    assert isinstance(store.commit(None, retained, owner, 0), Committed)
    assert store.release(owner, 0)
    with log_writer(project, sim) as writer:
        replay_store = RunLogReplayStore(writer, store, SimThreads())
        fence = replay_store.acquire("host-a", 0, 30)
        assert fence is not None
        with pytest.raises(ValueError, match="facts-only import"):
            replay_store.commit(None, envelope(1), fence, 0)
    assert store.load() == retained


def test_a_keyed_archive_in_the_atomic_store_grants_no_authority(tmp_path: Path, sim: Sim) -> None:
    """Regression: an old store archive must never replace run.log as the record authority."""
    project, _ = written_run(tmp_path, sim)
    store = project.state_store(RUN)
    owner = store.acquire("host-a", 0, 30)
    assert owner is not None
    archived = StoredEnvelope(
        revision=1, schema_version=REPLAY_STORE_SCHEMA_VERSION, payload=envelope(LENGTH).payload
    )
    assert isinstance(store.commit(0, archived, owner, 0), Committed)
    assert store.release(owner, 0)
    with pytest.raises(ValueError, match=r"only run\.log may hold them"):
        reload(project)
