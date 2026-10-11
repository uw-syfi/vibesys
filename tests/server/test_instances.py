"""The live-instance registry: proven liveness, self-cleaning listing, typed stop."""

from __future__ import annotations

import json
import os
import stat
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from pydantic import ValidationError

from server.api.protocol import Response
from server.control_envelope import CommandAck, StopCommand
from server.host_api import (
    RECORD_VERSION,
    FailureReason,
    InstanceList,
    InstanceStatus,
    LiveInstanceRecord,
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
    STOP_TIMEOUT_SECONDS,
    TOMBSTONE_RETENTION_SECONDS,
    FakeInstanceHold,
    FakeInstanceStore,
    FileInstanceStore,
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
from vs_sim.api.testing import HANG_GUARD_S, ManualClock, SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from server.host_api import InstanceStopResult
    from server.registry import InstanceStore
    from vs_sim.api import Listener

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

    assert (verdict is Verdict.LIVE) == (held and isinstance(record, LiveInstanceRecord))
    assert (verdict is Verdict.UNVERIFIED) == (lock is LockState.UNKNOWN)
    assert (verdict is Verdict.CRASHED) == (
        left and isinstance(record, LiveInstanceRecord) and record.ended_at is None
    )
    assert (verdict is Verdict.ENDED) == (
        left
        and isinstance(record, LiveInstanceRecord)
        and record.ended_at is not None
        and now - record.ended_at < TOMBSTONE_RETENTION_SECONDS
    )


# --- record versions ------------------------------------------------------------

_RECORD_DOCUMENTS = {
    "current": lambda instance_id: _record(instance_id).model_dump(mode="json"),
    "v2": lambda instance_id: {
        **_record(instance_id).model_dump(mode="json", exclude={"phase", "log_path"}),
        "version": 2,
    },
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
        assert (instance_id in live) == (kind == "current")


@pytest.mark.parametrize(
    ("kind", "version"),
    [
        ("v2", 2),
        ("future", 9),
        ("no version", None),
        ("not an object", None),
        ("corrupt", None),
        ("current, extra key", RECORD_VERSION),
    ],
)
def test_an_unreadable_record_names_the_version_it_claims(kind: str, version: int | None) -> None:
    assert parse_record(IDS[0], _raw(kind, IDS[0])) == UnreadableRecord(id=IDS[0], version=version)


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
        hold.release()
        self.ended[instance_id] = (self.clock.now(), phase)

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
            if now - ended_at < TOMBSTONE_RETENTION_SECONDS
        }

        live = [record.id for record in listing.instances]
        ended = [record.id for record in listing.ended]
        unreadable = [record.id for record in listing.unreadable]
        assert sorted(live) == sorted(self.alive & self.published)
        assert sorted(ended) == sorted(self.ended)
        assert sorted(unreadable) == sorted(self.foreign)
        assert len({*live, *ended, *unreadable}) == len(live) + len(ended) + len(unreadable)
        for record in (*listing.instances, *listing.ended):
            assert record.run_id == self.run_ids[record.id]
        for record in listing.ended:
            assert (record.ended_at, record.phase) == self.ended[record.id]
        # Listing keeps every live server and every fresh tombstone, and nothing else.
        assert set(self.store.ids()) == self.alive | self.foreign | set(self.ended)

    @invariant()
    def no_ended_server_is_live_and_no_live_one_is_lost(self) -> None:
        for instance_id in self.holds:
            verdict = judge(self.store.observe(instance_id), self.clock.now())
            if instance_id in self.alive:
                assert verdict is (
                    Verdict.LIVE if instance_id in self.published else Verdict.PENDING
                )
            else:
                assert verdict in {Verdict.ENDED, Verdict.CRASHED, Verdict.DEAD}

    def _record(self, instance_id: str) -> LiveInstanceRecord:
        return _record(instance_id, started_at=self.started_at, run_id=self.run_ids[instance_id])


TestRegistryInterleavings = RegistryInterleavings.TestCase


def test_an_unreadable_lock_is_reported_but_neither_trusted_nor_removed() -> None:
    store = FakeInstanceStore()
    store.hold(IDS[0]).publish(_record(IDS[0]))
    store.entries[IDS[0]].lock = LockState.UNKNOWN

    assert LiveRegistry(store, CLOCK).list() == InstanceList(instances=(), unverified=(IDS[0],))
    assert store.ids() == (IDS[0],)


# --- stop ---------------------------------------------------------------------

# A server whose control socket nothing answers: every stop below that uses it
# exercises the signal fallback.
_UNANSWERED = ControlSocketStopRequester(SimNetwork(SimThreads()))


class _ExitingSignaller:
    """A server that ends (cleanly or not) when signalled, or ignores the signal."""

    def __init__(self, on_terminate: Callable[[], None], *, supported: bool = True) -> None:
        self.on_terminate = on_terminate
        self.supported = supported
        self.signalled: list[int] = []

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
        IDS[0], StopEffects(_UNANSWERED, signaller, clock, clock.advance)
    )

    assert result.outcome is StopOutcome.STOPPED
    assert signaller.signalled == [4242]
    assert LiveRegistry(store, CLOCK).list().instances == ()


def test_stop_reports_a_server_that_outlives_the_wait(
    serving: tuple[FakeInstanceStore, FakeInstanceHold],
) -> None:
    store, _ = serving
    clock = ManualClock()

    result = LiveRegistry(store, CLOCK).stop(
        IDS[0], StopEffects(_UNANSWERED, _ExitingSignaller(lambda: None), clock, clock.advance)
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
            instance_id, StopEffects(_UNANSWERED, signaller, clock, clock.advance)
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
            _UNANSWERED, _ExitingSignaller(lambda: None, supported=False), clock, clock.advance
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
    UNBOUND = "unbound"


def _reply_bytes(reply: _Reply, request_id: str) -> bytes:
    match reply:
        case _Reply.ACK:
            response = Response(
                request_id=request_id, ack=CommandAck(action="stop", status="pending")
            )
        case _Reply.ERROR:
            response = Response(request_id=request_id, ok=False, error="read-only run")
        case _:
            return b"not json\n"
    return response.model_dump_json().encode() + b"\n"


def _stop_against(
    reply: _Reply, exit_after: float | None, *, force: bool = False
) -> tuple[InstanceStopResult, FakeInstanceStore, list[str], list[int]]:
    """Stop a live server that answers ``reply`` and exits ``exit_after`` seconds later.

    The server and the stopping client share one simulated network and clock;
    the record's socket path is only an address on that network. A signal
    ends the server at once, so whether it was sent shows in the outcome.
    """
    threads = SimThreads()
    network = SimNetwork(threads)
    store = FakeInstanceStore()
    hold = store.hold(IDS[0])
    record = _record(IDS[0])
    hold.publish(record)
    received: list[str] = []
    signaller = _ExitingSignaller(hold.release)

    def serve(listener: Listener) -> None:
        connection = listener.accept(HANG_GUARD_S)
        line = b""
        while not line.endswith(b"\n"):
            line += connection.recv(1, HANG_GUARD_S)
        request = StopCommand.model_validate_json(line)
        received.append(request.type)
        if reply is _Reply.SILENT:
            threads.sleep(2 * CONTROL_REPLY_TIMEOUT_SECONDS)
        elif reply is not _Reply.HANG_UP:
            connection.send(_reply_bytes(reply, request.request_id))
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
            StopEffects(ControlSocketStopRequester(network), signaller, threads, threads.sleep),
            force=force,
        )

    result = threads.run(scenario)
    return result, store, received, signaller.signalled


@given(exit_after=st.floats(min_value=0, max_value=STOP_TIMEOUT_SECONDS * 0.9))
def test_an_acknowledged_stop_waits_for_the_run_to_end_without_signalling(
    exit_after: float,
) -> None:
    result, store, received, signalled = _stop_against(_Reply.ACK, exit_after)

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
    result, store, _, signalled = _stop_against(_Reply.ACK, exit_after)

    assert (result.outcome, result.route) == (StopOutcome.STOPPING, StopRoute.CONTROL_SOCKET)
    assert signalled == []
    # The server exits on its own once its agent call ends; until then its
    # record stays, so `instances list` still shows it.
    assert store.ids() == (IDS[0],)


@pytest.mark.parametrize(
    "reply", [_Reply.ERROR, _Reply.GARBAGE, _Reply.HANG_UP, _Reply.SILENT, _Reply.UNBOUND]
)
def test_a_server_that_does_not_acknowledge_falls_back_to_the_signal(reply: _Reply) -> None:
    result, store, _, signalled = _stop_against(reply, exit_after=0.0)

    assert (result.outcome, result.route) == (StopOutcome.STOPPED, StopRoute.SIGNAL)
    assert signalled == [4242]
    assert LiveRegistry(store, CLOCK).list().instances == ()


def test_a_forced_stop_signals_without_asking() -> None:
    result, _, received, signalled = _stop_against(_Reply.ACK, exit_after=0.0, force=True)

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


def test_a_root_open_to_other_users_is_refused(tmp_path: Path) -> None:
    (tmp_path / "vibesys").mkdir(mode=0o755)
    (tmp_path / "vibesys").chmod(0o755)

    with pytest.raises(PermissionError, match="mode 0700"):
        instance_root({"XDG_RUNTIME_DIR": str(tmp_path)}, os.getuid())


def test_a_root_owned_by_another_user_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PermissionError, match="owned by uid"):
        instance_root({"XDG_RUNTIME_DIR": str(tmp_path)}, os.getuid() + 1)


def test_socket_paths_fit_the_unix_limit_with_room_to_spare() -> None:
    root = Path("/run/user/4294967294/vibesys")

    assert len(str(instance_socket_path(root, new_instance_id())).encode()) <= (
        MAX_SOCKET_PATH_BYTES - 40
    )
