"""The live-instance registry: proven liveness, self-cleaning listing, typed stop."""

from __future__ import annotations

import os
import stat
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule
from pydantic import ValidationError

from server.api.protocol import CommandAck, Response, StopCommand
from server.instances import (
    CONTROL_REPLY_TIMEOUT_SECONDS,
    STOP_TIMEOUT_SECONDS,
    ControlSocketStopRequester,
    FakeInstanceHold,
    FakeInstanceStore,
    FileInstanceHold,
    FileInstanceStore,
    InstanceList,
    InstanceObservation,
    InstanceStatus,
    LiveInstanceRecord,
    LiveRegistry,
    StopEffects,
    StopOutcome,
    StopRoute,
    Verdict,
    instance_root,
    instance_socket_path,
    judge,
    new_instance_id,
    parse_instance_id,
)
from server.transport.discovery import LockState
from server.transport.unix_jsonl import MAX_SOCKET_PATH_BYTES
from vs_sim.api.testing import HANG_GUARD_S, ManualClock, SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from server.instances import InstanceHold, InstanceStopResult, InstanceStore
    from vs_sim.api import Listener

IDS = [f"{index:012x}" for index in range(1, 6)]


def _record(instance_id: str, *, started_at: float = 1.0) -> LiveInstanceRecord:
    return LiveInstanceRecord(
        id=instance_id,
        status=InstanceStatus.SERVING,
        socket_path=f"/run/vibesys/runs/{instance_id}/control.sock",
        project_root="/project",
        pid=4242,
        started_at=started_at,
        hostname="node",
        vibesys_version="0+test",
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

    assert judge(store.observe(IDS[0])) is Verdict.PENDING
    hold.publish(_record(IDS[0]))
    assert store.observe(IDS[0]) == InstanceObservation(IDS[0], LockState.HELD, _record(IDS[0]))
    assert judge(store.observe(IDS[0])) is Verdict.LIVE


def test_a_live_holder_is_never_reaped(store: InstanceStore) -> None:
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))

    assert store.reap(IDS[0]) is False
    assert judge(store.observe(IDS[0])) is Verdict.LIVE


def test_a_crashed_holder_is_dead_and_reaped(store: InstanceStore) -> None:
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))
    _crash(hold)

    assert store.observe(IDS[0]).lock is LockState.FREE
    assert judge(store.observe(IDS[0])) is Verdict.DEAD
    assert store.reap(IDS[0]) is True
    assert store.ids() == ()


def test_release_removes_every_file(store: InstanceStore) -> None:
    hold = store.hold(IDS[0])
    hold.publish(_record(IDS[0]))
    hold.release()
    hold.release()

    assert store.ids() == ()
    assert store.observe(IDS[0]) == InstanceObservation(IDS[0], LockState.ABSENT, None)


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

    assert judge(store.observe(IDS[0])) is Verdict.PENDING


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
    assert judge(store.observe(IDS[0])) is Verdict.PENDING


# --- pure core ----------------------------------------------------------------


@given(
    lock=st.sampled_from(LockState),
    record=st.one_of(st.none(), st.sampled_from(IDS).map(_record)),
)
def test_only_a_held_lock_with_a_record_is_live(
    lock: LockState, record: LiveInstanceRecord | None
) -> None:
    verdict = judge(InstanceObservation(IDS[0], lock, record))

    assert (verdict is Verdict.LIVE) == (lock is LockState.HELD and record is not None)
    assert (verdict is Verdict.DEAD) == (lock in {LockState.FREE, LockState.ABSENT})


# --- registry under any interleaving -------------------------------------------


class RegistryInterleavings(RuleBasedStateMachine):
    """Start, publish, crash, release and list in any order, on both stores at once.

    Each operation is applied to the Fake and to the filesystem store, so the
    Fake is held to the real store's behavior as well as to the invariants.
    """

    def __init__(self) -> None:
        super().__init__()
        self._directory = tempfile.TemporaryDirectory()
        self.stores: tuple[InstanceStore, ...] = (
            FakeInstanceStore(),
            FileInstanceStore(Path(self._directory.name)),
        )
        self.holds: dict[str, tuple[InstanceHold, ...]] = {}
        self.crashed: set[str] = set()
        self.alive: set[str] = set()
        self.published: set[str] = set()
        self.started_at = 0.0

    @precondition(lambda self: len(self.holds) < len(IDS))
    @rule(data=st.data())
    def start(self, data: st.DataObject) -> None:
        instance_id = data.draw(st.sampled_from([i for i in IDS if i not in self.holds]))
        self.holds[instance_id] = tuple(store.hold(instance_id) for store in self.stores)
        self.alive.add(instance_id)

    @precondition(lambda self: bool(self.alive))
    @rule(data=st.data())
    def publish(self, data: st.DataObject) -> None:
        instance_id = data.draw(st.sampled_from(sorted(self.alive)))
        self.started_at += 1
        for hold in self.holds[instance_id]:
            hold.publish(_record(instance_id, started_at=self.started_at))
        self.published.add(instance_id)

    @precondition(lambda self: bool(self.alive))
    @rule(data=st.data(), ending=st.sampled_from(["release", "crash"]))
    def end(self, data: st.DataObject, ending: str) -> None:
        instance_id = data.draw(st.sampled_from(sorted(self.alive)))
        for hold in self.holds[instance_id]:
            if ending == "release":
                hold.release()
            else:
                _crash(hold)
        if ending == "crash":
            self.crashed.add(instance_id)
        self.alive.discard(instance_id)

    @rule()
    def list_instances(self) -> None:
        listings = [LiveRegistry(store).list() for store in self.stores]

        assert listings[0] == listings[1]
        listed = {record.id for record in listings[0].instances}
        assert listed == self.alive & self.published
        for store in self.stores:
            # Listing removes every dead id and keeps every live one.
            assert set(store.ids()) == self.alive

    @invariant()
    def no_dead_server_is_live_and_no_live_one_is_lost(self) -> None:
        for store in self.stores:
            for instance_id in IDS:
                verdict = judge(store.observe(instance_id))
                if instance_id in self.alive:
                    assert verdict in {Verdict.LIVE, Verdict.PENDING}
                    assert (verdict is Verdict.LIVE) == (instance_id in self.published)
                else:
                    assert verdict is Verdict.DEAD

    def teardown(self) -> None:
        for instance_id, holds in self.holds.items():
            if instance_id not in self.crashed:
                for hold in holds:
                    hold.release()
        self._directory.cleanup()


TestRegistryInterleavings = RegistryInterleavings.TestCase


def test_an_unreadable_lock_is_reported_but_neither_trusted_nor_removed() -> None:
    store = FakeInstanceStore()
    store.hold(IDS[0]).publish(_record(IDS[0]))
    store.entries[IDS[0]].lock = LockState.UNKNOWN

    assert LiveRegistry(store).list() == InstanceList(instances=(), unverified=(IDS[0],))
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

    result = LiveRegistry(store).stop(
        IDS[0], StopEffects(_UNANSWERED, signaller, clock, clock.advance)
    )

    assert result.outcome is StopOutcome.STOPPED
    assert signaller.signalled == [4242]
    assert store.ids() == ()


def test_stop_reports_a_server_that_outlives_the_wait(
    serving: tuple[FakeInstanceStore, FakeInstanceHold],
) -> None:
    store, _ = serving
    clock = ManualClock()

    result = LiveRegistry(store).stop(
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
        result = LiveRegistry(store).stop(
            instance_id, StopEffects(_UNANSWERED, signaller, clock, clock.advance)
        )
        assert result.outcome is StopOutcome.NOT_RUNNING
    assert signaller.signalled == []
    assert store.ids() == ()


def test_stop_reports_a_host_that_cannot_signal_safely(
    serving: tuple[FakeInstanceStore, FakeInstanceHold],
) -> None:
    store, _ = serving
    clock = ManualClock()

    result = LiveRegistry(store).stop(
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
        return LiveRegistry(store).stop(
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
    assert store.ids() == ()


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
    assert store.ids() == ()


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


def test_record_without_vibesys_root_still_parses_as_version_1() -> None:
    document = _record(IDS[0]).model_dump(exclude={"vibesys_root"})

    record = LiveInstanceRecord.model_validate(document)

    assert (record.version, record.vibesys_root) == (1, None)
