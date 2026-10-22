"""The live-instance registry: proven liveness, self-cleaning listing, typed stop."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from pydantic import ValidationError
from tests.server.support import CONTROL_KEY, REQUEST_TIME, operator_dialer

import server.registry as instance_module
from server.api.protocol import CommandAck, Response
from server.control_envelope import StopCommand
from server.host_api import (
    ECMASCRIPT_DATE_MAX_SECONDS,
    INSPECTION_LOG_TAIL_BYTES,
    RECORD_VERSION,
    AvailableLogTail,
    FailureReason,
    InspectionState,
    InstanceInspectionMetadata,
    InstanceList,
    InstanceLogTail,
    InstanceStatus,
    LiveInstanceRecord,
    MissingLogTail,
    PauseCause,
    RunPhase,
    RunStatus,
    StopOutcome,
    StopRoute,
    UnreadableRecord,
    parse_record,
)
from server.instances import CONTROL_REPLY_TIMEOUT_SECONDS, ControlSocketStopRequester
from server.registry import (
    CRASHED,
    EXIT_GRACE_SECONDS,
    STOP_TIMEOUT_SECONDS,
    TOMBSTONE_RETENTION_SECONDS,
    FakeInstanceHold,
    FakeInstanceLogs,
    FakeInstanceMetadataStore,
    FakeInstanceStore,
    FakeStopIntents,
    FileInstanceHold,
    FileInstanceLogs,
    FileInstanceMetadataStore,
    FileInstanceStore,
    InstanceHold,
    InstanceInspector,
    InstanceLogError,
    InstanceMetadataError,
    InstanceObservation,
    LiveRegistry,
    LockState,
    StopEffects,
    Verdict,
    crashed,
    instance_root,
    instance_socket_path,
    judge,
    new_instance_id,
    parse_instance_id,
)
from server.transport.unix_jsonl import MAX_SOCKET_PATH_BYTES
from vibesys.api import ControlAction
from vs_sim.api.testing import HANG_GUARD_S, ManualClock, SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from server.host_api import InstanceStopResult
    from server.registry import InstanceStore
    from vs_sim.api import Connection, Listener, Network

CLOCK = ManualClock()
IDS = [f"{index:012x}" for index in range(1, 6)]


def _ended(instance_id: str, *, ended_at: float) -> LiveInstanceRecord:
    return _record(instance_id).model_copy(
        update={"phase": RunPhase(status=RunStatus.STOPPED), "ended_at": ended_at}
    )


def _record(
    instance_id: str, *, started_at: float = 1.0, run_id: str = "run-1"
) -> LiveInstanceRecord:
    return LiveInstanceRecord(
        id=instance_id,
        status=InstanceStatus.SERVING,
        phase=RunPhase(status=RunStatus.RUNNING),
        socket_path=f"/run/vibesys/runs/{instance_id}/control.sock",
        log_path=f"/run/vibesys/runs/{instance_id}/server.log",
        project_root="/project",
        run_id=run_id,
        pid=4242,
        started_at=started_at,
        hostname="node",
        vibesys_version="0+test",
    )


def _inspection_metadata(
    instance_id: str = IDS[0], *, started_at: float = 1.0
) -> InstanceInspectionMetadata:
    return InstanceInspectionMetadata(
        id=instance_id,
        project_root="/project",
        run_id="20261011-120000-11111111-run",
        task="queue",
        pid=4242,
        started_at=started_at,
        hostname="node",
        vibesys_version="0+test",
        vibesys_root="/src/vibesys",
        control_socket_path=f"/run/vibesys/runs/{instance_id}/control.sock",
        server_log_path=f"/run/vibesys/runs/{instance_id}/server.log",
    )


def _crash(hold: InstanceHold) -> None:
    """Die without cleanup, as ``kill -9`` would: the lock drops, the files stay.

    A crashed hold must not be released afterwards: its descriptor is gone.
    """
    if isinstance(hold, FakeInstanceHold):
        hold.crash()
        return
    assert isinstance(hold, FileInstanceHold)
    # test-isolation: a real crash is the kernel closing the lock descriptor;
    # closing it here reproduces exactly that in process, without a child.
    os.close(hold._lock._descriptor or -1)  # noqa: SLF001  # lint-waiver: LW-178207 [SLF001]; model kill -9 by closing the private lock descriptor the kernel would close
    # > A public crash method on the production hold would exist only for tests,
    # > and a real child process per step would make the interleaving property slow.


@pytest.fixture(params=["fake", "file"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> InstanceStore:
    if request.param == "fake":
        return FakeInstanceStore()
    return FileInstanceStore(tmp_path)


# --- InstanceStore contract ---------------------------------------------------


def test_a_held_id_is_pending_until_its_record_is_published(store: InstanceStore) -> None:
    hold = store.hold(IDS[0])

    assert judge(store.observe(IDS[0]), 0.0) is Verdict.PENDING
    hold.publish(_record(IDS[0]))
    assert store.observe(IDS[0]) == InstanceObservation(IDS[0], LockState.HELD, _record(IDS[0]))
    assert judge(store.observe(IDS[0]), 0.0) is Verdict.LIVE


def test_a_live_holder_is_never_reaped(store: InstanceStore) -> None:
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))

    assert store.reap(IDS[0]) is False
    assert judge(store.observe(IDS[0]), 0.0) is Verdict.LIVE


def test_a_crashed_holder_is_buried_and_then_reaped() -> None:
    # A real kill -9 of a holder is tests/e2e/test_live_instances_e2e.py's.
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))
    hold.crash()

    assert store.observe(IDS[0]).lock is LockState.FREE
    assert judge(store.observe(IDS[0]), 0.0) is Verdict.CRASHED
    assert store.bury(crashed(_record(IDS[0]), 5.0)) is True
    assert judge(store.observe(IDS[0]), 5.0) is Verdict.ENDED
    assert store.reap(IDS[0]) is True
    assert store.ids() == ()


def test_release_leaves_the_last_record_as_the_tombstone(store: InstanceStore) -> None:
    tombstone = _ended(IDS[0], ended_at=5.0)
    hold = store.hold(IDS[0])
    hold.publish(tombstone)
    hold.release()
    hold.release()

    assert store.observe(IDS[0]) == InstanceObservation(IDS[0], LockState.ABSENT, tombstone)
    assert judge(store.observe(IDS[0]), 5.0 + TOMBSTONE_RETENTION_SECONDS - 1) is Verdict.ENDED
    assert judge(store.observe(IDS[0]), 5.0 + TOMBSTONE_RETENTION_SECONDS) is Verdict.DEAD
    assert store.bury(tombstone) is True
    assert store.reap(IDS[0]) is True
    assert store.ids() == ()


def test_a_live_holder_is_never_buried(store: InstanceStore) -> None:
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))

    assert store.bury(crashed(_record(IDS[0]), 5.0)) is False
    assert store.observe(IDS[0]).record == _record(IDS[0])


def test_an_id_cannot_be_held_twice(store: InstanceStore) -> None:
    store.hold(IDS[0])

    with pytest.raises(FileExistsError):
        store.hold(IDS[0])


@pytest.mark.parametrize("bad", ["", "../etc", "ABCDEF012345", "0123456789abc", "01234/6789ab"])
def test_a_malformed_id_never_reaches_the_filesystem(store: InstanceStore, bad: str) -> None:
    with pytest.raises(ValueError, match="must match"):
        store.observe(bad)
    with pytest.raises(ValueError, match="must match"):
        parse_instance_id(bad)


def test_a_record_naming_another_id_is_not_trusted(tmp_path: Path) -> None:
    store = FileInstanceStore(tmp_path)
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[1]))

    assert store.observe(IDS[0]).record == UnreadableRecord(id=IDS[0], version=RECORD_VERSION)
    assert judge(store.observe(IDS[0]), 0.0) is Verdict.UNREADABLE


def test_file_records_are_private_and_reject_unknown_keys(tmp_path: Path) -> None:
    store = FileInstanceStore(tmp_path)
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))
    record_path = tmp_path / "instances" / f"{IDS[0]}.json"

    assert stat.S_IMODE(record_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "instances").stat().st_mode) == 0o700
    raw = record_path.read_text().rstrip("}\n") + ', "extra": 1}'
    with pytest.raises(ValidationError, match="extra"):
        LiveInstanceRecord.model_validate_json(raw)
    record_path.write_text(raw)
    assert judge(store.observe(IDS[0]), 0.0) is Verdict.UNREADABLE


@pytest.mark.parametrize(
    "content",
    [b"\xff\xfe", b'{"version": 4, "id": "\xff"}', b"[" * 100_000],
    ids=["invalid-utf8", "invalid-utf8-in-a-string", "nested-past-the-recursion-limit"],
)
def test_a_corrupt_record_file_is_unreadable_not_an_error(tmp_path: Path, content: bytes) -> None:
    store = FileInstanceStore(tmp_path)
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))
    (tmp_path / "instances" / f"{IDS[0]}.json").write_bytes(content)

    assert store.observe(IDS[0]).record == UnreadableRecord(id=IDS[0], version=None)
    assert judge(store.observe(IDS[0]), 0.0) is Verdict.UNREADABLE


# --- inspection ---------------------------------------------------------------


@pytest.mark.parametrize("contract", [_record, _inspection_metadata])
def test_instance_timestamps_fit_the_ecmascript_date_domain(
    contract: Callable[..., LiveInstanceRecord | InstanceInspectionMetadata],
) -> None:
    assert contract(IDS[0], started_at=ECMASCRIPT_DATE_MAX_SECONDS).started_at == (
        ECMASCRIPT_DATE_MAX_SECONDS
    )
    with pytest.raises(ValidationError, match="less than or equal"):
        contract(IDS[0], started_at=ECMASCRIPT_DATE_MAX_SECONDS + 1)


@given(st.floats(min_value=0, max_value=ECMASCRIPT_DATE_MAX_SECONDS, allow_nan=False))
def test_every_finite_timestamp_in_the_ecmascript_date_domain_is_accepted(
    started_at: float,
) -> None:
    assert _record(IDS[0], started_at=started_at).started_at == started_at
    assert _inspection_metadata(IDS[0], started_at=started_at).started_at == started_at


def test_live_instance_record_does_not_absorb_inspection_metadata() -> None:
    assert set(_record(IDS[0]).model_dump()) == {
        "version",
        "id",
        "status",
        "phase",
        "socket_path",
        "log_path",
        "project_root",
        "run_id",
        "pid",
        "started_at",
        "ended_at",
        "paused",
        "end",
        "hostname",
        "protocol_version",
        "vibesys_version",
        "vibesys_root",
    }
    with pytest.raises(ValidationError, match="task"):
        LiveInstanceRecord.model_validate({**_record(IDS[0]).model_dump(), "task": "queue"})


def test_inspection_loads_diagnostics_only_when_explicitly_requested() -> None:
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))

    class RefusingLogs(FakeInstanceLogs):
        def tail(self, instance_id: str) -> InstanceLogTail:
            del instance_id
            raise AssertionError

    inspection = InstanceInspector(
        store, RefusingLogs(), FakeInstanceMetadataStore(), CLOCK
    ).inspect(IDS[0])

    assert inspection.state is InspectionState.LIVE
    assert inspection.record == _record(IDS[0])
    assert inspection.log is None


def test_inspection_observes_the_record_after_its_published_metadata() -> None:
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]).model_copy(update={"project_root": "/launch"}))
    authoritative = _record(IDS[0]).model_copy(update={"project_root": "/selected"})
    metadata = _inspection_metadata().model_copy(
        update={"project_root": "/selected", "run_id": authoritative.run_id}
    )

    class PublishingMetadataStore(FakeInstanceMetadataStore):
        def load(self, instance_id: str) -> InstanceInspectionMetadata | None:
            hold.publish(authoritative)
            return super().load(instance_id)

    snapshots = PublishingMetadataStore()
    snapshots.publish(metadata)

    inspection = InstanceInspector(store, FakeInstanceLogs(), snapshots, CLOCK).inspect(IDS[0])

    assert inspection.state is InspectionState.LIVE
    assert inspection.record == authoritative
    assert inspection.metadata == metadata
    assert inspection.record.project_root == inspection.metadata.project_root


def test_inspection_reports_ended_but_retains_metadata_when_the_run_ends_during_log_read() -> None:
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))

    class EndingLogs(FakeInstanceLogs):
        def tail(self, instance_id: str) -> InstanceLogTail:
            del instance_id
            hold.release()
            return AvailableLogTail(text="last line\n", truncated=False)

    inspection = InstanceInspector(store, EndingLogs(), FakeInstanceMetadataStore(), CLOCK).inspect(
        IDS[0], include_log=True
    )

    assert inspection.state is InspectionState.ENDED
    assert inspection.record == _record(IDS[0])
    assert inspection.log == AvailableLogTail(text="last line\n", truncated=False)


def test_inspection_does_not_trust_a_record_when_its_lock_is_unverified() -> None:
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))
    store.entries[IDS[0]].lock = LockState.UNKNOWN

    inspection = InstanceInspector(
        store, FakeInstanceLogs(), FakeInstanceMetadataStore(), CLOCK
    ).inspect(IDS[0])

    assert inspection.state is InspectionState.UNVERIFIED
    assert inspection.record is None


def test_ended_inspection_loads_the_retained_snapshot_without_a_live_record() -> None:
    metadata = FakeInstanceMetadataStore()
    metadata.publish(_inspection_metadata())

    inspection = InstanceInspector(
        FakeInstanceStore(), FakeInstanceLogs(), metadata, CLOCK
    ).inspect(IDS[0])

    assert inspection.state is InspectionState.ENDED
    assert inspection.record is None
    assert inspection.metadata == _inspection_metadata()
    assert inspection.server_log_path == _inspection_metadata().server_log_path


@given(raw=st.binary(max_size=512), limit=st.integers(min_value=1, max_value=256))
def test_every_fake_log_tail_is_valid_utf8_within_its_byte_bound(raw: bytes, limit: int) -> None:
    logs = FakeInstanceLogs(limit=limit)
    logs.contents[IDS[0]] = raw

    tail = logs.tail(IDS[0])

    assert isinstance(tail, AvailableLogTail)
    assert len(tail.text.encode("utf-8")) <= limit
    repaired_size = len(raw.decode("utf-8", errors="replace").encode("utf-8"))
    assert tail.truncated == (len(raw) > limit or repaired_size > limit)


def test_file_log_tail_reads_from_the_end_and_handles_a_multibyte_boundary(tmp_path: Path) -> None:
    path = tmp_path / "runs" / IDS[0] / "server.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old" * INSPECTION_LOG_TAIL_BYTES + "\néclair\n".encode())

    tail = FileInstanceLogs(tmp_path).tail(IDS[0])

    assert isinstance(tail, AvailableLogTail)
    assert tail.text.endswith("éclair\n")
    assert len(tail.text.encode("utf-8")) <= INSPECTION_LOG_TAIL_BYTES
    assert tail.truncated is True


@pytest.mark.parametrize("component", ["ancestor", "root", "runs", "instance"])
def test_file_log_tail_rejects_a_symlink_in_every_parent_component(
    tmp_path: Path, component: str
) -> None:
    outside = tmp_path / "outside"
    (outside / "runs" / IDS[0]).mkdir(parents=True)
    (outside / "runs" / IDS[0] / "server.log").write_text("secret")
    root = tmp_path / "registry"
    if component == "ancestor":
        parent = tmp_path / "parent"
        parent.mkdir()
        linked = parent / "linked"
        linked.symlink_to(outside, target_is_directory=True)
        root = linked / "registry"
        (outside / "registry" / "runs" / IDS[0]).mkdir(parents=True)
        (outside / "registry" / "runs" / IDS[0] / "server.log").write_text("secret")
    elif component == "root":
        root.symlink_to(outside, target_is_directory=True)
    elif component == "runs":
        root.mkdir()
        (root / "runs").symlink_to(outside / "runs", target_is_directory=True)
    else:
        (root / "runs").mkdir(parents=True)
        (root / "runs" / IDS[0]).symlink_to(outside / "runs" / IDS[0], target_is_directory=True)

    with pytest.raises(InstanceLogError, match="safely"):
        FileInstanceLogs(root).tail(IDS[0])


def test_an_opened_instance_directory_stays_anchored_when_its_root_is_replaced(
    tmp_path: Path,
) -> None:
    root = tmp_path / "registry"
    directory = root / "runs" / IDS[0]
    directory.mkdir(parents=True)
    (directory / "server.log").write_text("original")

    # test-isolation: this security property is the descriptor-walk mechanism itself. Opening the
    # private scope is the only deterministic way to replace a parent between the directory walk
    # and leaf open without a scheduling race or a test-only callback in the production interface.
    with instance_module._instance_directory(root, IDS[0]) as opened:  # noqa: SLF001  # lint-waiver: LW-182301 [SLF001]; exercise descriptor anchoring across an adversarial rename
        root.rename(tmp_path / "opened-registry")
        replacement = root / "runs" / IDS[0]
        replacement.mkdir(parents=True)
        (replacement / "server.log").write_text("replacement")
        descriptor = os.open(
            "server.log",
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=opened,
        )
        try:
            assert os.read(descriptor, 64) == b"original"
        finally:
            os.close(descriptor)


def test_a_file_one_byte_over_the_tail_bound_is_marked_truncated(tmp_path: Path) -> None:
    path = tmp_path / "runs" / IDS[0] / "server.log"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x" * (INSPECTION_LOG_TAIL_BYTES + 1))

    tail = FileInstanceLogs(tmp_path).tail(IDS[0])

    assert isinstance(tail, AvailableLogTail)
    assert len(tail.text.encode()) == INSPECTION_LOG_TAIL_BYTES
    assert tail.truncated is True


def test_a_missing_instance_log_is_a_typed_result(tmp_path: Path) -> None:
    assert FileInstanceLogs(tmp_path).tail(IDS[0]) == MissingLogTail()


def test_the_typed_log_contract_rejects_an_oversized_tail() -> None:
    with pytest.raises(ValidationError, match="log tail exceeds"):
        AvailableLogTail(text="x" * (INSPECTION_LOG_TAIL_BYTES + 1), truncated=True)


def test_log_readers_reject_a_limit_outside_the_public_contract(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="between 1"):
        FileInstanceLogs(tmp_path, limit=INSPECTION_LOG_TAIL_BYTES + 1)
    with pytest.raises(ValueError, match="between 1"):
        FakeInstanceLogs(limit=0)


def test_file_inspection_metadata_is_atomic_private_and_immutable(tmp_path: Path) -> None:
    directory = tmp_path / "runs" / IDS[0]
    directory.mkdir(parents=True)
    metadata = _inspection_metadata()
    snapshots = FileInstanceMetadataStore(tmp_path)

    snapshots.publish(metadata)
    snapshots.publish(metadata)

    path = directory / "inspection.json"
    assert snapshots.load(IDS[0]) == metadata
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(directory.glob(".*.tmp")) == []
    with pytest.raises(InstanceMetadataError, match="unlike immutable"):
        snapshots.publish(metadata.model_copy(update={"task": "other"}))


def test_file_inspection_metadata_rejects_a_symlinked_instance_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs" / IDS[0]).symlink_to(outside, target_is_directory=True)
    (outside / "inspection.json").write_text(_inspection_metadata().model_dump_json())

    snapshots = FileInstanceMetadataStore(tmp_path)
    with pytest.raises(InstanceMetadataError, match="safely"):
        snapshots.load(IDS[0])
    with pytest.raises(InstanceMetadataError, match="safely"):
        snapshots.publish(_inspection_metadata())


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "oversized", "unknown-field"])
def test_file_inspection_metadata_rejects_unsafe_or_invalid_snapshots(
    tmp_path: Path, kind: str
) -> None:
    path = tmp_path / "runs" / IDS[0] / "inspection.json"
    path.parent.mkdir(parents=True)
    if kind == "symlink":
        target = tmp_path / "outside"
        target.write_text(_inspection_metadata().model_dump_json())
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    elif kind == "fifo":
        os.mkfifo(path)
    elif kind == "oversized":
        path.write_bytes(b"x" * (32 * 1024 + 1))
    else:
        path.write_text(
            _inspection_metadata().model_dump_json().removesuffix("}") + ',"extra":true}'
        )

    with pytest.raises(InstanceMetadataError):
        FileInstanceMetadataStore(tmp_path).load(IDS[0])


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_file_log_tail_rejects_symlinks_and_non_regular_files(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "runs" / IDS[0] / "server.log"
    path.parent.mkdir(parents=True)
    if kind == "symlink":
        target = tmp_path / "secret"
        target.write_text("do not read")
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)

    with pytest.raises(InstanceLogError):
        FileInstanceLogs(tmp_path).tail(IDS[0])


# --- pure core ----------------------------------------------------------------


@given(
    lock=st.sampled_from(LockState),
    record=st.one_of(
        st.none(),
        st.sampled_from(IDS).map(_record),
        st.builds(_ended, st.sampled_from(IDS), ended_at=st.floats(0, 3 * 86_400)),
        st.builds(UnreadableRecord, id=st.just(IDS[0]), version=st.none() | st.integers()),
    ),
    now=st.floats(0, 3 * 86_400),
)
def test_only_a_held_lock_is_live_and_only_a_fresh_tombstone_is_ended(
    lock: LockState, record: LiveInstanceRecord | UnreadableRecord | None, now: float
) -> None:
    verdict = judge(InstanceObservation(IDS[0], lock, record), now)
    held = lock is LockState.HELD
    left = lock in {LockState.FREE, LockState.ABSENT}
    tombstone = isinstance(record, LiveInstanceRecord) and record.ended_at is not None

    assert (verdict is Verdict.LIVE) == (
        held and isinstance(record, LiveInstanceRecord) and not tombstone
    )
    assert (verdict is Verdict.UNVERIFIED) == (lock is LockState.UNKNOWN)
    assert (verdict is Verdict.CRASHED) == (
        left and isinstance(record, LiveInstanceRecord) and record.ended_at is None
    )
    # A holder seen with its tombstone is on its way out (or wedged there):
    # exiting at any age while it holds, so ``stop`` can still act on it.
    assert (verdict is Verdict.EXITING) == (held and tombstone)
    assert (verdict is Verdict.ENDED) == (
        tombstone
        and isinstance(record, LiveInstanceRecord)
        and record.ended_at is not None
        and left
        and now - record.ended_at < TOMBSTONE_RETENTION_SECONDS
    )


# --- record versions ------------------------------------------------------------

_RECORD_DOCUMENTS = {
    "current": lambda instance_id: _record(instance_id).model_dump(mode="json"),
    "v2": lambda instance_id: {
        **_record(instance_id).model_dump(mode="json", exclude={"phase", "log_path"}),
        "version": 2,
    },
    "v3": lambda instance_id: {**_record(instance_id).model_dump(mode="json"), "version": 3},
    "v3 with progress": lambda instance_id: {
        **_record(instance_id).model_dump(mode="json"),
        "version": 3,
        "phase": {
            "status": "starting",
            "stage": "building_image",
            "progress": {"done": 1, "total": 3, "label": "RUN npm install"},
        },
    },
    "v4": lambda instance_id: {**_record(instance_id).model_dump(mode="json"), "version": 4},
    "v4 with paused": lambda instance_id: {
        **_paused_record(instance_id).model_dump(mode="json"),
        "version": 4,
    },
    "current paused": lambda instance_id: _paused_record(instance_id).model_dump(mode="json"),
    "future": lambda instance_id: {"version": 9, "id": instance_id, "phase": "anything"},
    "no version": lambda instance_id: {"id": instance_id},
    "not an object": lambda _instance_id: [1, 2],
    "current, extra key": lambda instance_id: {
        **_record(instance_id).model_dump(mode="json"),
        "extra": 1,
    },
}


def _raw(kind: str, instance_id: str) -> str:
    return json.dumps(_RECORD_DOCUMENTS[kind](instance_id)) if kind != "corrupt" else "{"


@given(
    kinds=st.lists(st.sampled_from([*_RECORD_DOCUMENTS, "corrupt"]), min_size=1, max_size=len(IDS))
)
def test_a_listing_over_mixed_record_versions_reports_every_live_id_exactly_once(
    kinds: list[str],
) -> None:
    store = FakeInstanceStore()
    for instance_id, kind in zip(IDS, kinds, strict=False):
        store.plant(instance_id, _raw(kind, instance_id))

    listing = LiveRegistry(store, CLOCK).list()

    live = [record.id for record in listing.instances]
    unreadable = [record.id for record in listing.unreadable]
    assert sorted(live + unreadable) == IDS[: len(kinds)]
    for instance_id, kind in zip(IDS, kinds, strict=False):
        assert (instance_id in live) == (kind in {"current", "current paused", "v3", "v4"})


@pytest.mark.parametrize(
    ("kind", "version"),
    [
        ("v2", 2),
        ("v3 with progress", 3),
        ("v4 with paused", 4),
        ("future", 9),
        ("no version", None),
        ("not an object", None),
        ("corrupt", None),
        ("current, extra key", RECORD_VERSION),
    ],
)
def test_an_unreadable_record_names_the_version_it_claims(kind: str, version: int | None) -> None:
    assert parse_record(IDS[0], _raw(kind, IDS[0])) == UnreadableRecord(id=IDS[0], version=version)


@pytest.mark.parametrize("kind", ["v3", "v4"])
def test_an_older_record_reads_as_the_same_record_at_the_current_version(kind: str) -> None:
    assert parse_record(IDS[0], _raw(kind, IDS[0])) == _record(IDS[0])


def _paused_record(instance_id: str) -> LiveInstanceRecord:
    return _record(instance_id).model_copy(
        update={"phase": RunPhase(status=RunStatus.PAUSED), "paused": PauseCause.QUOTA}
    )


def test_a_newer_checkouts_tombstone_is_left_to_its_own_lister() -> None:
    store = FakeInstanceStore()
    newer = json.dumps({"version": RECORD_VERSION + 1, "id": IDS[0]})
    store.plant(IDS[0], newer, lock=LockState.ABSENT)
    store.plant(IDS[1], json.dumps({"version": RECORD_VERSION - 1, "id": IDS[1]}), LockState.FREE)

    listing = LiveRegistry(store, CLOCK).list()

    assert listing.unreadable == (UnreadableRecord(id=IDS[0], version=RECORD_VERSION + 1),)
    assert store.ids() == (IDS[0],)


_ENDINGS = {
    "completed": RunPhase(status=RunStatus.COMPLETED),
    "stopped": RunPhase(status=RunStatus.STOPPED),
    "failed": RunPhase(
        status=RunStatus.FAILED, reason=FailureReason(code="boom", stage="run", message="boom")
    ),
}


class RegistryInterleavings(RuleBasedStateMachine):
    """Start, publish, end (cleanly or by crash), let time pass, and list, in any order.

    Each server is named its run when it starts, as a launcher does, and newer
    checkouts plant records this reader cannot interpret. Whatever the order:
    a listed record names the run its server started with; every id with
    something to report is listed exactly once (live, ended, or unreadable);
    an ended record keeps the phase it ended in, a crashed one is reported as
    crashed, and a tombstone is listed for a day and then removed.
    """

    def __init__(self) -> None:
        super().__init__()
        self.clock = ManualClock(1_000.0)
        self.store = FakeInstanceStore()
        self.holds: dict[str, FakeInstanceHold] = {}
        self.run_ids: dict[str, str] = {}
        self.alive: set[str] = set()
        self.published: set[str] = set()
        self.foreign: set[str] = set()
        self.crashed: set[str] = set()
        """Crashed after publishing; buried by the next listing."""
        self.ended: dict[str, tuple[float, RunPhase]] = {}
        self.exiting: set[str] = set()
        """Wrote a tombstone and still hold the lock."""
        self.started_at = 0.0

    def _fresh(self) -> list[str]:
        return [i for i in IDS if i not in self.holds and i not in self.foreign]

    @precondition(lambda self: bool(self._fresh()))
    @rule(data=st.data())
    def start(self, data: st.DataObject) -> None:
        instance_id = data.draw(st.sampled_from(self._fresh()))
        self.holds[instance_id] = self.store.hold(instance_id)
        self.run_ids[instance_id] = f"run-{instance_id}"
        self.alive.add(instance_id)

    @precondition(lambda self: bool(self._fresh()))
    @rule(data=st.data(), version=st.integers(min_value=RECORD_VERSION + 1, max_value=9))
    def a_newer_checkout_starts(self, data: st.DataObject, version: int) -> None:
        instance_id = data.draw(st.sampled_from(self._fresh()))
        self.store.plant(instance_id, json.dumps({"version": version, "id": instance_id}))
        self.foreign.add(instance_id)

    @precondition(lambda self: bool(self.alive))
    @rule(data=st.data())
    def publish(self, data: st.DataObject) -> None:
        instance_id = data.draw(st.sampled_from(sorted(self.alive)))
        self.started_at += 1
        self.holds[instance_id].publish(self._record(instance_id))
        self.published.add(instance_id)

    @precondition(lambda self: bool(self.alive))
    @rule(data=st.data(), ending=st.sampled_from([*_ENDINGS, "crash"]))
    def end(self, data: st.DataObject, ending: str) -> None:
        instance_id = data.draw(st.sampled_from(sorted(self.alive)))
        hold = self.holds[instance_id]
        self.alive.discard(instance_id)
        if ending == "crash":
            hold.crash()
            if instance_id in self.published:
                self.crashed.add(instance_id)
            return
        phase = _ENDINGS[ending]
        hold.publish(
            self._record(instance_id).model_copy(
                update={"phase": phase, "ended_at": self.clock.now()}
            )
        )
        self.exiting.add(instance_id)
        self.ended[instance_id] = (self.clock.now(), phase)

    @precondition(lambda self: bool(self.exiting))
    @rule(data=st.data(), crash=st.booleans())
    def exit(self, data: st.DataObject, *, crash: bool) -> None:
        """Drop the lock after the tombstone, as ``kill -9`` during cleanup would too.

        Until this runs the server holds its lock with its tombstone published,
        which is also what a lister sees when the server ends between the lock
        probe and the record read.
        """
        instance_id = data.draw(st.sampled_from(sorted(self.exiting)))
        self.exiting.discard(instance_id)
        if crash:
            self.holds[instance_id].crash()
        else:
            self.holds[instance_id].release()

    @rule(hours=st.integers(min_value=0, max_value=30))
    def time_passes(self, hours: int) -> None:
        self.clock.advance(hours * 3_600.0)

    @rule()
    def list_instances(self) -> None:
        now = self.clock.now()
        listing = LiveRegistry(self.store, self.clock).list()
        for instance_id in self.crashed:
            self.ended[instance_id] = (now, RunPhase(status=RunStatus.FAILED, reason=CRASHED))
        self.crashed.clear()
        self.ended = {
            instance_id: (ended_at, phase)
            for instance_id, (ended_at, phase) in self.ended.items()
            if instance_id in self.exiting or now - ended_at < TOMBSTONE_RETENTION_SECONDS
        }
        wedged = self._wedged(now)

        live = [record.id for record in listing.instances]
        ended = [record.id for record in listing.ended]
        unreadable = [record.id for record in listing.unreadable]
        assert sorted(live) == sorted((self.alive & self.published) | wedged)
        assert sorted(ended) == sorted(set(self.ended) - wedged)
        assert sorted(unreadable) == sorted(self.foreign)
        assert len({*live, *ended, *unreadable}) == len(live) + len(ended) + len(unreadable)
        for record in (*listing.instances, *listing.ended):
            assert record.run_id == self.run_ids[record.id]
        for record in (*listing.ended, *(r for r in listing.instances if r.id in wedged)):
            assert (record.ended_at, record.phase) == self.ended[record.id]
        # Listing keeps every live server and every fresh tombstone, and nothing else.
        assert set(self.store.ids()) == self.alive | self.foreign | set(self.ended)

    @rule()
    def read_instances(self) -> None:
        """A watch's read reports what a listing would, and writes nothing.

        A crashed server not yet buried is reported crashed without
        ``ended_at``; an expired tombstone is not reported but stays on disk.
        """
        now = self.clock.now()
        before = {key: (entry.lock, entry.raw) for key, entry in self.store.entries.items()}

        listing = LiveRegistry(self.store, self.clock).read()

        assert {key: (entry.lock, entry.raw) for key, entry in self.store.entries.items()} == before
        expected_ended: dict[str, tuple[float | None, RunPhase]] = {
            instance_id: (ended_at, phase)
            for instance_id, (ended_at, phase) in self.ended.items()
            if instance_id in self.exiting or now - ended_at < TOMBSTONE_RETENTION_SECONDS
        }
        for instance_id in self.crashed:
            expected_ended[instance_id] = (None, RunPhase(status=RunStatus.FAILED, reason=CRASHED))
        wedged = self._wedged(now)
        for instance_id in wedged:
            del expected_ended[instance_id]
        assert sorted(record.id for record in listing.instances) == sorted(
            (self.alive & self.published) | wedged
        )
        assert {
            record.id: (record.ended_at, record.phase) for record in listing.ended
        } == expected_ended
        assert sorted(record.id for record in listing.unreadable) == sorted(self.foreign)

    @invariant()
    def no_ended_server_is_live_and_no_live_one_is_lost(self) -> None:
        for instance_id in self.holds:
            verdict = judge(self.store.observe(instance_id), self.clock.now())
            if instance_id in self.alive:
                assert verdict is (
                    Verdict.LIVE if instance_id in self.published else Verdict.PENDING
                )
            elif instance_id in self.exiting:
                assert verdict is Verdict.EXITING
            else:
                assert verdict in {Verdict.ENDED, Verdict.CRASHED, Verdict.DEAD}

    def _wedged(self, now: float) -> set[str]:
        """Holders still exiting past the grace: listed as live, so they can be stopped."""
        return {
            instance_id
            for instance_id in self.exiting
            if now - self.ended[instance_id][0] >= EXIT_GRACE_SECONDS
        }

    def _record(self, instance_id: str) -> LiveInstanceRecord:
        return _record(instance_id, started_at=self.started_at, run_id=self.run_ids[instance_id])


TestRegistryInterleavings = RegistryInterleavings.TestCase


class _EndsBetweenProbeAndRead:
    """An ``InstanceStore`` whose one server ends between the lock probe and the record read.

    ``FileInstanceStore.observe`` is two reads, the lock and then the record,
    and nothing stops the owner from writing its tombstone in between. This
    store composes the same two reads over a Fake and runs the owner's ending
    between them on the first observation, so the race is a fixed schedule.
    """

    def __init__(self, store: FakeInstanceStore, ending: Callable[[], None]) -> None:
        self._store = store
        self._ending: Callable[[], None] | None = ending

    def ids(self) -> tuple[str, ...]:
        return self._store.ids()

    def observe(self, instance_id: str) -> InstanceObservation:
        lock = self._store.observe(instance_id).lock
        ending, self._ending = self._ending, None
        if ending is not None:
            ending()
        return InstanceObservation(instance_id, lock, self._store.observe(instance_id).record)

    def reap(self, instance_id: str) -> bool:
        return self._store.reap(instance_id)

    def bury(self, record: LiveInstanceRecord) -> bool:
        return self._store.bury(record)

    def hold(self, instance_id: str) -> FakeInstanceHold:
        return self._store.hold(instance_id)


@pytest.mark.parametrize("ending", sorted(_ENDINGS))
@pytest.mark.parametrize("reader", ["list", "read", "find", "inspect"])
def test_a_server_that_ends_between_the_lock_probe_and_the_record_read_is_not_live(
    ending: str, reader: str
) -> None:
    fake = FakeInstanceStore()
    hold = fake.hold(IDS[0])
    hold.publish(_record(IDS[0]))
    tombstone = _record(IDS[0]).model_copy(
        update={"phase": _ENDINGS[ending], "ended_at": CLOCK.now()}
    )
    store = _EndsBetweenProbeAndRead(fake, lambda: hold.publish(tombstone))
    registry = LiveRegistry(store, CLOCK)

    match reader:
        case "list" | "read":
            listing = registry.list() if reader == "list" else registry.read()
            assert listing.instances == ()
            assert listing.ended == (tombstone,)
        case "find":
            assert registry.find(IDS[0]) is None
        case _:
            inspection = InstanceInspector(
                store, FakeInstanceLogs(), FakeInstanceMetadataStore(), CLOCK
            ).inspect(IDS[0])
            assert inspection.state is InspectionState.ENDED
            assert inspection.record is None


def test_an_unreadable_lock_is_reported_but_neither_trusted_nor_removed() -> None:
    store = FakeInstanceStore()
    store.hold(IDS[0]).publish(_record(IDS[0]))
    store.entries[IDS[0]].lock = LockState.UNKNOWN

    assert LiveRegistry(store, CLOCK).list() == InstanceList(instances=(), unverified=(IDS[0],))
    assert store.ids() == (IDS[0],)


# --- stop ---------------------------------------------------------------------

# A server whose control socket nothing answers: every stop below that uses it
# exercises the signal fallback.
_UNANSWERED_THREADS = SimThreads()
_UNANSWERED = ControlSocketStopRequester(
    operator_dialer(SimNetwork(_UNANSWERED_THREADS)),
    clock=_UNANSWERED_THREADS,
    deadline_clock=_UNANSWERED_THREADS,
)


class _ExitingSignaller:
    """A server that ends (cleanly or not) when signalled, or ignores the signal."""

    def __init__(self, on_terminate: Callable[[], None], *, supported: bool = True) -> None:
        self.on_terminate = on_terminate
        self.supported = supported
        self.signalled: list[int] = []

    def exists(self, pid: int) -> bool:
        return pid not in self.signalled

    def terminate_if_current(self, pid: int, current: Callable[[], bool]) -> bool:
        if not self.supported:
            raise NotImplementedError
        if not current():
            return False
        self.signalled.append(pid)
        self.on_terminate()
        return True


@pytest.fixture
def serving() -> Iterator[tuple[FakeInstanceStore, FakeInstanceHold]]:
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))
    yield store, hold
    hold.release()


@pytest.mark.parametrize("ends", ["release", "crash"])
def test_stop_signals_a_live_server_and_waits_for_its_lock(
    serving: tuple[FakeInstanceStore, FakeInstanceHold], ends: str
) -> None:
    store, hold = serving
    signaller = _ExitingSignaller(hold.release if ends == "release" else hold.crash)
    clock = ManualClock()

    result = LiveRegistry(store, CLOCK).stop(
        IDS[0], StopEffects(_UNANSWERED, signaller, clock, clock.advance, FakeStopIntents())
    )

    assert result.outcome is StopOutcome.STOPPED
    assert signaller.signalled == [4242]
    assert LiveRegistry(store, CLOCK).list().instances == ()


def test_a_signalled_stop_is_recorded_durably_before_the_signal(
    serving: tuple[FakeInstanceStore, FakeInstanceHold],
) -> None:
    """A force stop keeps the stop request first: the killed server cannot commit it."""
    store, hold = serving
    intents = FakeStopIntents()
    seen_at_signal: list[list[str]] = []

    def terminated() -> None:
        seen_at_signal.append(list(intents.recorded))
        hold.release()

    clock = ManualClock()
    result = LiveRegistry(store, CLOCK).stop(
        IDS[0],
        StopEffects(_UNANSWERED, _ExitingSignaller(terminated), clock, clock.advance, intents),
        force=True,
    )

    assert result.outcome is StopOutcome.STOPPED
    assert seen_at_signal == [["run-1"]]


def test_a_stop_that_signals_nothing_records_nothing() -> None:
    store = FakeInstanceStore()
    intents = FakeStopIntents()
    clock = ManualClock()

    result = LiveRegistry(store, CLOCK).stop(
        IDS[0],
        StopEffects(_UNANSWERED, _ExitingSignaller(lambda: None), clock, clock.advance, intents),
    )

    assert result.outcome is StopOutcome.NOT_RUNNING
    assert intents.recorded == []


def test_stop_reports_a_server_that_outlives_the_wait(
    serving: tuple[FakeInstanceStore, FakeInstanceHold],
) -> None:
    store, _ = serving
    clock = ManualClock()

    result = LiveRegistry(store, CLOCK).stop(
        IDS[0],
        StopEffects(
            _UNANSWERED, _ExitingSignaller(lambda: None), clock, clock.advance, FakeStopIntents()
        ),
    )

    assert result.outcome is StopOutcome.STILL_RUNNING
    assert store.ids() == (IDS[0],)


def test_stop_never_signals_a_dead_or_unknown_server(
    serving: tuple[FakeInstanceStore, FakeInstanceHold],
) -> None:
    store, hold = serving
    hold.crash()
    signaller = _ExitingSignaller(lambda: None)
    clock = ManualClock()

    for instance_id in (IDS[0], IDS[1]):
        result = LiveRegistry(store, CLOCK).stop(
            instance_id,
            StopEffects(_UNANSWERED, signaller, clock, clock.advance, FakeStopIntents()),
        )
        assert result.outcome is StopOutcome.NOT_RUNNING
    assert signaller.signalled == []
    assert LiveRegistry(store, CLOCK).list().instances == ()


def test_stop_reports_a_host_that_cannot_signal_safely(
    serving: tuple[FakeInstanceStore, FakeInstanceHold],
) -> None:
    store, _ = serving
    clock = ManualClock()

    result = LiveRegistry(store, CLOCK).stop(
        IDS[0],
        StopEffects(
            _UNANSWERED,
            _ExitingSignaller(lambda: None, supported=False),
            clock,
            clock.advance,
            FakeStopIntents(),
        ),
    )

    assert result.outcome is StopOutcome.UNSUPPORTED
    assert store.ids() == (IDS[0],)


class _Reply(StrEnum):
    """What a simulated server answers to one stop request."""

    ACK = "ack"
    ERROR = "error"
    GARBAGE = "garbage"
    HANG_UP = "hang_up"
    SILENT = "silent"
    TRICKLE = "trickle"
    UNBOUND = "unbound"


@dataclass(frozen=True)
class _StopDelays:
    connect: float = 0.0
    request_read: float = 0.0
    trickle: float = 0.0


_NO_STOP_DELAYS = _StopDelays()


class _DelayedConnectNetwork:
    """Consume virtual time before delegating, respecting the caller's connect bound."""

    def __init__(self, network: Network, threads: SimThreads, delay: float) -> None:
        self._network = network
        self._threads = threads
        self._delay = delay

    def listen(self, address: str) -> Listener:
        return self._network.listen(address)

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        waited = self._delay if timeout is None else min(self._delay, timeout)
        self._threads.sleep(waited)
        if timeout is not None and self._delay >= timeout:
            message = "connection did not open before its deadline"
            raise TimeoutError(message)
        return self._network.connect(address, timeout)


def _reply_bytes(reply: _Reply, request_id: str) -> bytes:
    match reply:
        case _Reply.ACK:
            response = Response(
                timestamp=REQUEST_TIME,
                request_id=request_id,
                ack=CommandAck(action=ControlAction.PAUSE_NOW, status="pending"),
            )
        case _Reply.ERROR:
            response = Response(
                timestamp=REQUEST_TIME, request_id=request_id, ok=False, error="read-only run"
            )
        case _:
            return b"not json\n"
    return response.model_dump_json().encode() + b"\n"


def _answer_stop(
    connection: Connection,
    reply: _Reply,
    request_id: str,
    threads: SimThreads,
    trickle_interval: float,
) -> None:
    """Produce the simulated server behavior selected by ``reply``."""
    if reply is _Reply.SILENT:
        threads.sleep(2 * CONTROL_REPLY_TIMEOUT_SECONDS)
        return
    if reply is _Reply.TRICKLE:
        for _ in range(3):
            try:
                connection.send(b"x")
            except OSError:
                break
            threads.sleep(trickle_interval)
        return
    if reply is not _Reply.HANG_UP:
        connection.send(_reply_bytes(reply, request_id))


def _stop_against(
    reply: _Reply,
    exit_after: float | None,
    *,
    force: bool = False,
    delays: _StopDelays = _NO_STOP_DELAYS,
) -> tuple[InstanceStopResult, FakeInstanceStore, list[str], list[int], float]:
    """Stop a live server that answers ``reply`` and exits ``exit_after`` seconds later.

    The server and the stopping client share one simulated network and clock;
    the record's socket path is only an address on that network. A signal
    ends the server at once, so whether it was sent shows in the outcome.
    """
    threads = SimThreads()
    simulated = (
        SimNetwork(threads, buffer_bytes=8) if reply is _Reply.TRICKLE else SimNetwork(threads)
    )
    network: Network = (
        simulated
        if delays.connect == 0.0
        else _DelayedConnectNetwork(simulated, threads, delays.connect)
    )
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    record = _record(IDS[0])
    hold.publish(record)
    received: list[str] = []
    signaller = _ExitingSignaller(hold.release)

    def serve(listener: Listener) -> None:
        connection = listener.accept(HANG_GUARD_S)
        if reply is _Reply.TRICKLE:
            threads.sleep(delays.request_read)
        hello = b""
        while not hello.endswith(b"\n"):
            hello += connection.recv(1, HANG_GUARD_S)
        assert CONTROL_KEY.admits(hello), hello
        line = b""
        while not line.endswith(b"\n"):
            line += connection.recv(1, HANG_GUARD_S)
        request = StopCommand.model_validate_json(line)
        received.append(request.type)
        _answer_stop(connection, reply, request.request_id, threads, delays.trickle)
        connection.close()
        if reply is _Reply.ACK and exit_after is not None:
            threads.sleep(exit_after)
            hold.release()

    def scenario() -> InstanceStopResult:
        if reply is not _Reply.UNBOUND:
            listener = network.listen(record.socket_path)
            threads.spawn(lambda: serve(listener), name="server", daemon=True)
        return LiveRegistry(store, CLOCK).stop(
            IDS[0],
            StopEffects(
                ControlSocketStopRequester(
                    operator_dialer(network), clock=threads, deadline_clock=threads
                ),
                signaller,
                threads,
                threads.sleep,
                FakeStopIntents(),
            ),
            force=force,
        )

    started = threads.now()
    result = threads.run(scenario)
    return result, store, received, signaller.signalled, threads.now() - started


@given(exit_after=st.floats(min_value=0, max_value=STOP_TIMEOUT_SECONDS * 0.9))
def test_an_acknowledged_stop_waits_for_the_run_to_end_without_signalling(
    exit_after: float,
) -> None:
    result, store, received, signalled, _elapsed = _stop_against(_Reply.ACK, exit_after)

    assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.CONTROL_SOCKET)
    assert received == ["command.stop"]
    assert signalled == []
    assert LiveRegistry(store, CLOCK).list().instances == ()


@given(
    exit_after=st.one_of(
        st.none(),
        st.floats(min_value=STOP_TIMEOUT_SECONDS * 1.1, max_value=10 * STOP_TIMEOUT_SECONDS),
    )
)
def test_a_run_still_in_its_agent_call_reports_stopping_and_stays_registered(
    exit_after: float | None,
) -> None:
    result, store, _, signalled, _elapsed = _stop_against(_Reply.ACK, exit_after)

    assert (result.outcome, result.route) == (StopOutcome.STOPPING, StopRoute.CONTROL_SOCKET)
    assert signalled == []
    # The server exits on its own once its agent call ends; until then its
    # record stays, so `instances list` still shows it.
    assert store.ids() == (IDS[0],)


@pytest.mark.parametrize(
    "reply", [_Reply.ERROR, _Reply.GARBAGE, _Reply.HANG_UP, _Reply.SILENT, _Reply.UNBOUND]
)
def test_a_server_that_does_not_acknowledge_falls_back_to_the_signal(reply: _Reply) -> None:
    result, store, _, signalled, _elapsed = _stop_against(reply, exit_after=0.0)

    assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.SIGNAL)
    assert signalled == [4242]
    assert LiveRegistry(store, CLOCK).list().instances == ()


def test_an_incomplete_trickle_reaches_signal_fallback_at_the_whole_call_deadline() -> None:
    result, store, received, signalled, elapsed = _stop_against(
        _Reply.TRICKLE,
        exit_after=0.0,
        delays=_StopDelays(request_read=2.0, trickle=2.0),
    )

    assert elapsed == CONTROL_REPLY_TIMEOUT_SECONDS
    assert received == ["command.stop"]
    assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.SIGNAL)
    assert signalled == [4242]
    assert LiveRegistry(store, CLOCK).list().instances == ()


def test_connect_delay_reduces_the_send_and_reply_budget() -> None:
    result, _store, received, signalled, elapsed = _stop_against(
        _Reply.TRICKLE,
        exit_after=0.0,
        delays=_StopDelays(connect=2.0, request_read=1.0, trickle=2.0),
    )

    assert elapsed == CONTROL_REPLY_TIMEOUT_SECONDS
    assert received == ["command.stop"]
    assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.SIGNAL)
    assert signalled == [4242]


def test_connect_that_exhausts_the_call_deadline_falls_back_at_that_deadline() -> None:
    result, _store, received, signalled, elapsed = _stop_against(
        _Reply.ACK,
        exit_after=0.0,
        delays=_StopDelays(connect=CONTROL_REPLY_TIMEOUT_SECONDS),
    )

    assert elapsed == CONTROL_REPLY_TIMEOUT_SECONDS
    assert received == []
    assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.SIGNAL)
    assert signalled == [4242]


@given(
    request_read_delay=st.floats(
        min_value=CONTROL_REPLY_TIMEOUT_SECONDS * 0.1,
        max_value=CONTROL_REPLY_TIMEOUT_SECONDS * 0.4,
    ),
    trickle_interval=st.floats(
        min_value=CONTROL_REPLY_TIMEOUT_SECONDS * 0.31,
        max_value=CONTROL_REPLY_TIMEOUT_SECONDS * 0.45,
    ),
)
def test_request_send_and_every_partial_read_share_one_deadline(
    request_read_delay: float, trickle_interval: float
) -> None:
    result, _store, _received, signalled, elapsed = _stop_against(
        _Reply.TRICKLE,
        exit_after=0.0,
        delays=_StopDelays(request_read=request_read_delay, trickle=trickle_interval),
    )

    assert elapsed == CONTROL_REPLY_TIMEOUT_SECONDS
    assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.SIGNAL)
    assert signalled == [4242]


def test_a_forced_stop_signals_without_asking() -> None:
    result, _, received, signalled, _elapsed = _stop_against(_Reply.ACK, exit_after=0.0, force=True)

    assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.SIGNAL)
    assert received == []
    assert signalled == [4242]


# --- root ---------------------------------------------------------------------


def test_the_root_lives_under_the_runtime_directory_and_is_private(
    tmp_path: Path,
) -> None:
    root = instance_root({"XDG_RUNTIME_DIR": str(tmp_path)}, os.getuid())

    assert root == tmp_path / "vibesys"
    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_a_root_others_can_write_is_refused(tmp_path: Path) -> None:
    (tmp_path / "vibesys").mkdir(mode=0o775)
    (tmp_path / "vibesys").chmod(0o775)

    with pytest.raises(PermissionError, match="not writable by group or other"):
        instance_root({"XDG_RUNTIME_DIR": str(tmp_path)}, os.getuid())


def test_a_root_others_can_only_read_is_made_private(tmp_path: Path) -> None:
    (tmp_path / "vibesys").mkdir(mode=0o755)
    (tmp_path / "vibesys").chmod(0o755)

    root = instance_root({"XDG_RUNTIME_DIR": str(tmp_path)}, os.getuid())

    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_a_root_owned_by_another_user_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="owned by uid"):
        instance_root({"XDG_RUNTIME_DIR": str(tmp_path)}, os.getuid() + 1)


def test_socket_paths_fit_the_unix_limit_with_room_to_spare() -> None:
    root = Path("/run/user/4294967294/vibesys")

    assert len(str(instance_socket_path(root, new_instance_id())).encode()) <= (
        MAX_SOCKET_PATH_BYTES - 40
    )


@pytest.mark.parametrize("opening", ["[", '{"version":'])
def test_a_record_nested_past_the_recursion_limit_is_unreadable_not_a_crash(opening: str) -> None:
    """``json.loads`` recursed per level and raised ``RecursionError`` out of a listing."""
    raw = opening * 100_000
    assert parse_record(IDS[0], raw) == UnreadableRecord(id=IDS[0], version=None)


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize(
    "tombstone_age", [0.0, EXIT_GRACE_SECONDS, TOMBSTONE_RETENTION_SECONDS * 2]
)
def test_stop_signals_a_server_wedged_after_its_tombstone(
    *, force: bool, tombstone_age: float
) -> None:
    """A holder that wrote its tombstone but never let go is still a process to stop.

    Its control socket no longer serves, so ``stop`` waits out the stop bound
    for it to let go by itself, then signals it; ``force``, or a tombstone past
    the exit grace, signals at once.
    Treating it as not running left no way to end it through the tool.
    """
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_ended(IDS[0], ended_at=0.0))
    signaller = _ExitingSignaller(hold.release)
    clock = ManualClock()
    registry = LiveRegistry(store, ManualClock(tombstone_age))

    result = registry.stop(
        IDS[0],
        StopEffects(_UNANSWERED, signaller, clock, clock.advance, FakeStopIntents()),
        force=force,
    )

    assert result.outcome is StopOutcome.STOPPED
    assert result.route is StopRoute.SIGNAL
    assert signaller.signalled == [4242]
    if force or tombstone_age >= EXIT_GRACE_SECONDS:
        # Forced, or past the exit grace: a wedged holder is signalled at once.
        assert clock.now() == 0.0
    else:
        assert clock.now() >= STOP_TIMEOUT_SECONDS


def test_stop_lets_an_exiting_server_finish_without_a_signal() -> None:
    """A holder that lets go within the stop bound stopped by itself."""
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_ended(IDS[0], ended_at=0.0))
    signaller = _ExitingSignaller(lambda: None)
    clock = ManualClock()

    def pause(seconds: float) -> None:
        clock.advance(seconds)
        hold.release()

    result = LiveRegistry(store, CLOCK).stop(
        IDS[0], StopEffects(_UNANSWERED, signaller, clock, pause, FakeStopIntents())
    )

    assert result.outcome is StopOutcome.STOPPED
    assert signaller.signalled == []


@pytest.mark.parametrize(
    ("age", "listed_live"),
    [(0.0, False), (EXIT_GRACE_SECONDS - 0.001, False), (EXIT_GRACE_SECONDS, True), (3600.0, True)],
)
def test_a_holder_past_the_exit_grace_lists_as_live_again(*, age: float, listed_live: bool) -> None:
    """Ended while it is plausibly exiting; past the grace, a wedged process the operator sees."""
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_ended(IDS[0], ended_at=0.0))

    listing = LiveRegistry(store, ManualClock(age)).read()

    assert [record.id for record in listing.instances] == ([IDS[0]] if listed_live else [])
    assert [record.id for record in listing.ended] == ([] if listed_live else [IDS[0]])


@pytest.mark.parametrize(
    "age",
    [0.0, EXIT_GRACE_SECONDS - 0.001, EXIT_GRACE_SECONDS, 3_600.0, TOMBSTONE_RETENTION_SECONDS * 2],
)
def test_every_reader_agrees_whether_a_holder_with_its_tombstone_is_running(age: float) -> None:
    """A listing and an inspection agree, ``find`` never offers it, and ``stop`` waits only within the grace.

    A holder still holding its lock ``EXIT_GRACE_SECONDS`` after its tombstone
    is wedged in its exit and lists as running, so an operator can stop it.
    The inspector once reported it not running at any age, and ``stop`` waited
    out ``STOP_TIMEOUT_SECONDS`` for it to let go even when its tombstone was
    hours old. Its run has ended, so ``find`` (what ``run.connect`` and attach
    use) reports it gone at every age: dialing it answered ``CONNECT_FAILED``,
    which a client retries, instead of ``RUN_GONE``.
    """
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    hold.publish(_ended(IDS[0], ended_at=0.0))
    clock = ManualClock(age)
    registry = LiveRegistry(store, clock)
    running = age >= EXIT_GRACE_SECONDS

    listed = [record.id for record in registry.list().instances]
    found = registry.find(IDS[0])
    inspection = InstanceInspector(
        store, FakeInstanceLogs(), FakeInstanceMetadataStore(), clock
    ).inspect(IDS[0])

    assert (listed == [IDS[0]]) is running
    assert found is None
    assert (inspection.state is InspectionState.LIVE) is running

    signaller = _ExitingSignaller(hold.release)
    waits = ManualClock()
    result = registry.stop(
        IDS[0],
        StopEffects(_UNANSWERED, signaller, waits, waits.advance, FakeStopIntents()),
        force=False,
    )
    assert result.outcome is StopOutcome.STOPPED
    assert (waits.now() >= STOP_TIMEOUT_SECONDS) is not running
