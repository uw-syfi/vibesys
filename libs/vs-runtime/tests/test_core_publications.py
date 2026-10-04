"""Production and Fake publication contracts, including append-before-ack replay."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_core.api import ClockAdvanced, ContractError, OperationRegistry
from vs_project.api import FakeStateStore, Project
from vs_runtime.api.core import JournalPublicationDelivery, Publication, PublicationContext
from vs_runtime.api.testing import FakePublicationDelivery

from .test_core_shell import runtime

pytestmark = pytest.mark.asyncio

if TYPE_CHECKING:
    from pathlib import Path


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
        JournalPublicationDelivery(namespace, OperationRegistry(), store)
        if production
        else FakePublicationDelivery(store)
    )
    # Obtain a token through the public lease API on a separate run epoch.
    token = store.acquire("next", now=10, duration=10)
    assert token is not None
    context = PublicationContext(fence=token, now_at=10)
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
        await delivery.publish(publication, PublicationContext(fence=token, now_at=20))
    assert delivery.read() == (publication,)


async def test_append_before_ack_restart_deduplicates_and_only_advances_storage() -> None:
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=10)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    delivery = FakePublicationDelivery(store)
    first = shell.record.pending_publications[0]

    class AppendThenCrash:
        async def publish(self, publication: Publication, context: PublicationContext) -> None:
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
    delivery = FakePublicationDelivery(store)
    core = shell.record.envelope.core
    assert await shell.run_until_idle(delivery, now_at=count) is None
    assert shell.record.envelope.core == core
    assert len(delivery.read()) == count
    assert shell.record.delivery_cursor == count
    assert await shell.run_until_idle(delivery, now_at=count) is None
    assert len(delivery.read()) == count
