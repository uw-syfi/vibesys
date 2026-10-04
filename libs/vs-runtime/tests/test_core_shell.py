"""Shell contracts via public core traces, while canonical ledger/scheduling land."""

from __future__ import annotations

from types import UnionType
from typing import Annotated, get_args, get_origin

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.runtime_core_shell import CounterState, CounterStrategy, runtime

from vs_core.api import (
    ClockAdvanced,
    EventCursor,
    HostFence,
    HostId,
    Request,
    RunEnvelope,
    initial_state,
)
from vs_project.api import CommitFault, FakeStateStore, StoredEnvelope
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    RuntimeCommitError,
    RuntimeCommitUncertainError,
    RuntimeRecord,
)


def request_variants(annotation: object) -> set[type]:
    if hasattr(annotation, "__value__"):
        return request_variants(annotation.__value__)
    origin = get_origin(annotation)
    if origin is Annotated:
        return request_variants(get_args(annotation)[0])
    if origin is UnionType:
        return set().union(*(request_variants(child) for child in get_args(annotation)))
    assert isinstance(annotation, type)
    return {annotation}


def test_dispatch_table_is_exhaustive() -> None:
    assert set(REQUEST_DISPATCH) == request_variants(Request)
    assert len(REQUEST_DISPATCH) == 24


@given(times=st.lists(st.integers(min_value=1, max_value=20), max_size=15))
def test_every_committed_input_reloads_the_exact_envelope(times: list[int]) -> None:
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=100)
    for index, time in enumerate(sorted(times), start=1):
        shell.submit(ClockAdvanced(now_at=time), now_at=time)
        assert shell.advance()
        stored = store.load()
        assert isinstance(stored, StoredEnvelope)
        restored = RuntimeRecord[CounterState].model_validate_json(stored.payload)
        assert restored == shell.record
        assert restored.envelope.strategy.callbacks == index
        assert restored.envelope.event_cursor.sequence == index
        assert len(restored.pending_publications) == index
        assert shell.storage_revision == index
    assert not shell.advance()


@pytest.mark.parametrize(
    "fault", [CommitFault.UNKNOWN_BEFORE, CommitFault.UNKNOWN_AFTER, CommitFault.UNKNOWN_SYNC]
)
def test_unknown_startup_commit_reloads_but_grants_no_dispatch(fault: CommitFault) -> None:
    store = FakeStateStore(fault_plan=(fault,))
    shell = runtime(store)
    with pytest.raises(RuntimeCommitUncertainError) as error:
        shell.start("host", now_at=0, lease_duration=100)
    assert error.value.candidate_visible == (fault != CommitFault.UNKNOWN_BEFORE)
    with pytest.raises(RuntimeCommitError):
        shell.decide(now_at=1)


def test_proposed_state_commits_with_receipts_and_callbacks() -> None:
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=100)
    shell.decide(now_at=1)
    assert shell.advance()
    assert shell.record.envelope.strategy.proposals == 1
    assert shell.record.envelope.core.revision == 2
    assert shell.record.envelope.event_cursor == EventCursor(sequence=0)


def test_unknown_record_fields_fail_before_lease_acquisition() -> None:
    store = FakeStateStore()
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    envelope = RunEnvelope[CounterState](
        schema_version=3,
        fence=HostFence(host_id=HostId(root="writer"), epoch=1),
        strategy_id=CounterStrategy().declaration.strategy_id,
        state_schema=CounterStrategy().declaration.state_schema,
        core=initial_state(),
        strategy=CounterStrategy().state,
        event_cursor=EventCursor(sequence=0),
    )
    payload = (
        RuntimeRecord[CounterState].fresh(envelope).model_dump_json()[:-1] + ',"unexpected":1}'
    )
    store.commit(
        None, StoredEnvelope(revision=0, schema_version=1, payload=payload.encode()), fence, now=0
    )
    with pytest.raises(ValidationError, match="unexpected"):
        runtime(store).start("reader", now_at=1, lease_duration=1)
    assert store.acquire("reader", now=1, duration=1) is not None
