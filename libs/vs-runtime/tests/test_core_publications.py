"""Production and Fake publication contracts, including append-before-ack replay."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import ClockAdvanced, ContractError, OperationRegistry
from vs_project.api import (
    FakeStateStore,
    Project,
    StateDocumentDamagedError,
    StateNamespace,
    StateStore,
    StoreFence,
)
from vs_runtime.api.core import (
    JournalPublicationDelivery,
    Publication,
    PublicationAcknowledgement,
    PublicationContext,
    RuntimeCommitError,
    RuntimeCommitUncertainError,
)
from vs_runtime.api.core_shell_fixtures import LostAcknowledgementStateStore, runtime
from vs_runtime.api.testing import FakePublicationDelivery
from vs_sim.api import OsThreads
from vs_sim.api.testing import HANG_GUARD_S, start_thread, wait_until_started_sync

pytestmark = pytest.mark.asyncio

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from vs_sim.api import Event


class _FenceRaceStore(FakeStateStore):
    """Expose which public fence path the delivery used to order the regression."""

    def __init__(self) -> None:
        super().__init__()
        self.fenced_mutation_used = False

    def mutate_if_fenced(self, fence: StoreFence, now: float, mutation: Callable[[], None]) -> bool:
        self.fenced_mutation_used = True
        return super().mutate_if_fenced(fence, now, mutation)


@dataclass(frozen=True)
class _FencedLease:
    """A lease that judges each write with one fence at one time, as a host would."""

    store: StateStore
    fence: StoreFence
    now_at: float

    def renew(self, *, now_at: float, lease_duration: float) -> None:
        del now_at, lease_duration

    def verify(self, *, now_at: float) -> bool:
        return self.store.verify(self.fence, max(now_at, self.now_at))

    def write_if_held(self, mutation: Callable[[], None]) -> bool:
        return self.store.mutate_if_fenced(self.fence, self.now_at, mutation)


def _context(store: StateStore, fence: StoreFence, now_at: float) -> PublicationContext:
    return PublicationContext(now_at=now_at, lease=_FencedLease(store, fence, now_at))


@dataclass(frozen=True)
class _RaceGates:
    read: Event
    takeover_attempted: Event
    begin_takeover: Future[None]
    takeover_done: Future[None]
    release_guarded: Future[None]


class _PausingReadNamespace:
    """A faithful namespace that retains one old read until the chosen ordering."""

    def __init__(
        self,
        real: StateNamespace,
        store: _FenceRaceStore,
        gates: _RaceGates,
    ) -> None:
        self._real = real
        self._store = store
        self._gates = gates

    def read_bytes(self, relative_path: str) -> bytes | None:
        source = self._real.read_bytes(relative_path)
        self._gates.read.set()
        if self._store.fenced_mutation_used:
            self._gates.release_guarded.result(timeout=HANG_GUARD_S)
        else:
            self._gates.takeover_done.result(timeout=HANG_GUARD_S)
        return source

    def write_bytes(self, relative_path: str, contents: bytes) -> None:
        self._real.write_bytes(relative_path, contents)


@pytest.mark.parametrize("production", [False, True], ids=["fake", "journal"])
async def test_publish_replay_conflict_sequence_and_epoch(
    tmp_path: Path, *, production: bool
) -> None:
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=10)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    delivery = (
        JournalPublicationDelivery(namespace, OperationRegistry())
        if production
        else FakePublicationDelivery()
    )
    # Obtain a token through the public lease API on a separate run epoch.
    token = store.acquire("next", now=10, duration=10)
    assert token is not None
    context = _context(store, token, 10)
    publication = shell.record.pending_publications[0]
    await delivery.publish(publication, context)
    await delivery.publish(publication, context)
    assert delivery.read() == (publication,)
    conflict = publication.model_copy(
        update={"event": publication.event.model_copy(update={"detail": "changed"})}
    )
    with pytest.raises(ContractError, match="publication_id"):
        await delivery.publish(conflict, context)
    with pytest.raises(ContractError, match="sequence"):
        await delivery.publish(
            publication.model_copy(update={"sequence": 3, "publication_id": "run:3"}), context
        )
    next_token = store.acquire("third", now=20, duration=10)
    assert next_token is not None
    with pytest.raises(ContractError, match="fence"):
        await delivery.publish(publication, _context(store, token, 20))
    assert delivery.read() == (publication,)


async def test_takeover_cannot_overwrite_a_current_hosts_acknowledged_publication(
    tmp_path: Path,
) -> None:
    source_store = FakeStateStore()
    shell = runtime(source_store)
    shell.start("source", now_at=0, lease_duration=100)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    shell.submit(ClockAdvanced(now_at=2), now_at=2)
    shell.advance()
    first, second = shell.record.pending_publications

    store = _FenceRaceStore()
    old_fence = store.acquire("old", now=0, duration=20)
    assert old_fence is not None
    real_namespace = Project.open(tmp_path).state.state_store_namespace("run")
    threads = OsThreads()
    gates = _RaceGates(
        read=threads.event(),
        takeover_attempted=threads.event(),
        begin_takeover=Future(),
        takeover_done=Future(),
        release_guarded=Future(),
    )
    paused_namespace = _PausingReadNamespace(real_namespace, store, gates)
    registry = OperationRegistry()
    old_delivery = JournalPublicationDelivery(cast("StateNamespace", paused_namespace), registry)
    current_delivery = JournalPublicationDelivery(real_namespace, registry)

    def publish_old() -> PublicationAcknowledgement:
        return asyncio.run(old_delivery.publish(first, _context(store, old_fence, 19)))

    def take_over_and_publish() -> tuple[PublicationAcknowledgement, PublicationAcknowledgement]:
        gates.takeover_attempted.set()
        gates.begin_takeover.result(timeout=HANG_GUARD_S)
        try:
            current_fence = store.acquire("current", now=20, duration=10)
            assert current_fence is not None
            context = _context(store, current_fence, 20)
            first_ack = asyncio.run(current_delivery.publish(first, context))
            second_ack = asyncio.run(current_delivery.publish(second, context))
            return first_ack, second_ack
        finally:
            gates.takeover_done.set_result(None)

    old_result = start_thread(publish_old)
    try:
        wait_until_started_sync(gates.read, old_result)
        current_result = start_thread(take_over_and_publish)
        wait_until_started_sync(gates.takeover_attempted, current_result)
        gates.begin_takeover.set_result(None)
        if store.fenced_mutation_used:
            gates.release_guarded.set_result(None)
    except BaseException:
        if not gates.begin_takeover.done():
            gates.begin_takeover.set_result(None)
        if not gates.release_guarded.done():
            gates.release_guarded.set_result(None)
        raise
    current_acknowledgements = current_result.result(timeout=HANG_GUARD_S)
    old_acknowledgement = old_result.result(timeout=HANG_GUARD_S)

    assert old_acknowledgement.sequence == 1
    assert tuple(ack.sequence for ack in current_acknowledgements) == (1, 2)
    assert tuple(row.sequence for row in current_delivery.read()) == (1, 2)


async def test_append_before_ack_restart_deduplicates_and_only_advances_storage() -> None:
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=10)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    delivery = FakePublicationDelivery()
    first = shell.record.pending_publications[0]

    class AppendThenCrash:
        async def publish(
            self, publication: Publication, context: PublicationContext
        ) -> PublicationAcknowledgement:
            await delivery.publish(publication, context)
            message = "crash after append before acknowledgement"
            raise OSError(message)

    before = shell.record
    with pytest.raises(OSError, match="crash after append"):
        await shell.publish_one(AppendThenCrash(), now_at=1)
    assert shell.record == before
    assert delivery.read() == (first,)
    restarted = runtime(store)
    restarted.start("next", now_at=10, lease_duration=10)
    core_revision = restarted.record.envelope.core.revision
    cas_revision = restarted.storage_revision
    assert cas_revision is not None
    assert await restarted.publish_one(delivery, now_at=10)
    assert restarted.record.envelope.core.revision == core_revision
    assert restarted.storage_revision == cas_revision + 1
    assert restarted.record.delivery_cursor == first.sequence
    assert restarted.record.pending_publications == ()
    assert delivery.read() == (first,)


@given(count=st.integers(min_value=0, max_value=12))
async def test_publication_drain_acknowledges_every_committed_callback_once(count: int) -> None:
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=100)
    for index in range(count):
        shell.submit(ClockAdvanced(now_at=index), now_at=index)
        shell.advance()
    delivery = FakePublicationDelivery()
    core = shell.record.envelope.core
    assert await shell.run_until_idle(delivery, now_at=count) is None
    assert shell.record.envelope.core == core
    assert len(delivery.read()) == count
    assert shell.record.delivery_cursor == count
    assert await shell.run_until_idle(delivery, now_at=count) is None
    assert len(delivery.read()) == count


async def test_unknown_ack_commit_halts_without_duplicate_publication() -> None:
    store = LostAcknowledgementStateStore(unknown_revision=2)
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=10)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    delivery = FakePublicationDelivery()
    with pytest.raises(RuntimeCommitUncertainError) as failure:
        await shell.publish_one(delivery, now_at=1)
    assert failure.value.candidate_visible
    assert len(delivery.read()) == 1
    with pytest.raises(RuntimeCommitError):
        await shell.publish_one(delivery, now_at=1)
    restarted = runtime(store)
    restarted.start("second", now_at=10, lease_duration=10)
    assert not await restarted.publish_one(delivery, now_at=10)
    assert len(delivery.read()) == 1


@pytest.mark.parametrize(
    ("contents", "expected"),
    [
        (b'{"schema_version":1,"publications":[', "publications.json"),
        (b"\xff\xfe secret-journal-bytes", "publications.json"),
        (b'{"schema_version":2,"publications":[]}', "version 2.*reads version 1"),
    ],
    ids=["truncated", "not_utf8", "newer_version"],
)
async def test_a_damaged_journal_is_a_typed_error_that_does_not_echo_it(
    tmp_path: Path, contents: bytes, expected: str
) -> None:
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    namespace.write_bytes("publications.json", contents)
    delivery = JournalPublicationDelivery(namespace, OperationRegistry())

    with pytest.raises(StateDocumentDamagedError, match=expected) as failure:
        delivery.read()
    assert "secret-journal-bytes" not in str(failure.value)


async def test_the_journal_decodes_a_document_once_however_often_it_is_read_or_replayed(
    tmp_path: Path,
) -> None:
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=10)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    namespace = Project.open(tmp_path).state.state_store_namespace("run")
    decodes: list[None] = []
    delivery = JournalPublicationDelivery(
        namespace, OperationRegistry(), on_decode=lambda: decodes.append(None)
    )
    token = store.acquire("next", now=10, duration=10)
    assert token is not None
    context = _context(store, token, 10)
    publication = shell.record.pending_publications[0]

    for _ in range(5):
        await delivery.publish(publication, context)
        assert len(delivery.read()) == 1
    # Only the journal this delivery did not write itself costs a decode.
    assert not decodes

    other = JournalPublicationDelivery(
        namespace, OperationRegistry(), on_decode=lambda: decodes.append(None)
    )
    assert len(other.read()) == 1
    assert len(other.read()) == 1
    assert len(decodes) == 1
