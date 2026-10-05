"""Shell contracts via public core traces, while canonical ledger/scheduling land."""

from __future__ import annotations

import json
from types import UnionType
from typing import Annotated, Any, get_args, get_origin

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.runtime_core_shell import (
    CounterState,
    CounterStrategy,
    ShellTraceTransitions,
    runtime,
)

from vs_core.api import (
    Access,
    ClockAdvanced,
    CoreState,
    EventCursor,
    HostFence,
    HostId,
    OperationRegistry,
    Request,
    RoleId,
    RunEnvelope,
    Scope,
    SessionId,
    SessionPhase,
    SessionSpec,
    SessionsState,
    SessionView,
    StrategyState,
    initial_state,
)
from vs_project.api import CommitFault, FakeStateStore, StoredEnvelope, StoreFence
from vs_runtime.api.core import (
    REQUEST_DISPATCH,
    CoreRuntime,
    CoreRuntimeBindings,
    OrphanWaitError,
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
        # The commit path no longer decodes what it wrote, so this is where the codec
        # properties live: the persisted bytes decode (through the registry codec, as a
        # resume does) to the committed record, and re-encoding gives the same bytes in
        # both the stored form and the canonical envelope form.
        registry = OperationRegistry()
        decoded = RuntimeRecord[CounterState].decode(stored, registry)
        assert decoded == shell.record
        assert decoded.model_dump_json().encode() == stored.payload
        assert registry.encode_envelope(decoded.envelope) == registry.encode_envelope(
            shell.record.envelope
        )
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


def test_default_core_runs_scheduling_events_on_the_real_kernel() -> None:
    """Scheduling is implemented, so the default core starts and commits clock events."""
    store = FakeStateStore()
    shell = CoreRuntime(store, CounterStrategy(), initial_state())
    shell.start("host", now_at=0, lease_duration=10)
    assert store.load() is not None
    started = shell.record
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    assert shell.advance()
    assert shell.record.envelope.core.run.now_at == 1
    assert shell.record.envelope.core.revision >= started.envelope.core.revision


def test_renewal_changes_only_lease_authority() -> None:
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=2)
    before = shell.record
    revision = shell.storage_revision
    token = shell.renew(now_at=1, lease_duration=100)
    assert token.epoch == before.envelope.fence.epoch
    assert shell.record == before
    assert shell.storage_revision == revision
    shell.submit(ClockAdvanced(now_at=3), now_at=3)
    assert shell.advance()


@pytest.mark.parametrize("fault", list(CommitFault))
def test_failed_or_uncertain_renewal_grants_no_dispatch(fault: CommitFault) -> None:
    store = FakeStateStore(lease_fault_plan=(None, fault))
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=2)
    before = store.load()
    with pytest.raises(OSError, match="state-store"):
        shell.renew(now_at=1, lease_duration=10)
    assert store.load() == before
    with pytest.raises(RuntimeCommitError):
        shell.decide(now_at=2)


def test_definite_startup_write_failure_never_admits() -> None:
    store = FakeStateStore(fault_plan=(CommitFault.FAILED,))
    shell = runtime(store)
    with pytest.raises(OSError, match="state-store"):
        shell.start("host", now_at=0, lease_duration=10)
    assert store.load() is None
    with pytest.raises(RuntimeCommitError):
        shell.decide(now_at=1)


def test_lost_fence_commit_reloads_and_never_admits() -> None:
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=1)
    before = store.load()
    assert store.acquire("second", now=1, duration=10) is not None
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    with pytest.raises(RuntimeCommitError, match="fence"):
        shell.advance()
    assert store.load() == before
    with pytest.raises(RuntimeCommitError):
        shell.decide(now_at=1)


@pytest.mark.parametrize("version", [True, False, 1.0, "1", 0, 2])
def test_shell_schema_version_rejects_coercion_and_unknown_versions(version: object) -> None:
    shell = runtime(FakeStateStore())
    shell.start("host", now_at=0, lease_duration=10)
    value = json.loads(shell.record.model_dump_json())
    value["schema_version"] = version
    with pytest.raises(ValidationError, match="schema_version"):
        RuntimeRecord[CounterState].model_validate_json(json.dumps(value))


@given(
    stamps=st.lists(st.integers(min_value=0, max_value=20), min_size=1, max_size=12),
)
def test_an_input_stamped_before_an_earlier_commit_still_commits(stamps: list[int]) -> None:
    """A tool call that arrives during a turn is stamped before the turn's own commit.

    The store's time watermark never moves back, so the shell must stamp every commit
    and every lease check no earlier than the last commit, whatever order stamps arrive in.
    """
    store = FakeStateStore()
    shell = runtime(store)
    shell.start("host", now_at=0, lease_duration=1000)
    for event_time, stamp in enumerate(stamps, start=1):
        shell.submit(ClockAdvanced(now_at=event_time), now_at=stamp)
        assert shell.advance()
        assert shell.holds_lease(now_at=stamp)
    assert shell.record.envelope.core.run.now_at == len(stamps)


class CountingRegistry(OperationRegistry):
    """The real codec, counting whole-envelope encodes and decodes."""

    def __init__(self) -> None:
        super().__init__()
        self.encodes = 0
        self.decodes = 0
        self.validations = 0

    def encode_envelope(self, envelope: RunEnvelope[Any]) -> str:
        self.encodes += 1
        return super().encode_envelope(envelope)

    def decode_envelope[S: StrategyState](
        self, model: type[RunEnvelope[S]], source: str
    ) -> RunEnvelope[S]:
        self.decodes += 1
        return super().decode_envelope(model, source)

    def validate_envelope(self, envelope: RunEnvelope[Any]) -> None:
        self.validations += 1
        super().validate_envelope(envelope)


@given(times=st.lists(st.integers(min_value=1, max_value=20), min_size=1, max_size=10))
def test_a_commit_validates_once_and_never_round_trips_the_envelope(times: list[int]) -> None:
    registry = CountingRegistry()
    store = FakeStateStore()
    shell = CoreRuntime(
        store,
        CounterStrategy(),
        initial_state(),
        bindings=CoreRuntimeBindings(registry=registry, transitions=ShellTraceTransitions()),
    )
    shell.start("host", now_at=0, lease_duration=100)
    registry.encodes = registry.decodes = registry.validations = 0
    for index, time in enumerate(sorted(times), start=1):
        shell.submit(ClockAdvanced(now_at=time), now_at=time)
        assert shell.advance()
        assert shell.storage_revision is not None
        assert registry.validations == index
        assert registry.encodes == 0
        assert registry.decodes == 0


def _closing_session_with_nothing_to_end_it() -> CoreState:
    state = initial_state()
    session = SessionView(
        spec=SessionSpec(
            session_id=SessionId(root="orphan"),
            role_id=RoleId(root="worker"),
            policy="reuse",
            lifetime="owner",
            access=Access.WRITE_ARTIFACTS,
        ),
        scope=Scope(owner=state.run.run_id, generation=0),
        generation=0,
        phase=SessionPhase.CLOSING,
    )
    return state.model_copy(update={"sessions": SessionsState(sessions=(session,))})


def test_a_host_that_starts_over_a_run_with_an_orphan_wait_refuses_it() -> None:
    """A stored run that waits on something nothing will end halts at start, not later as an idle stall."""
    store = FakeStateStore()
    fence = store.acquire("writer", now=0, duration=1)
    assert fence is not None
    strategy = CounterStrategy()
    envelope = RunEnvelope[CounterState](
        schema_version=3,
        fence=HostFence(host_id=HostId(root="writer"), epoch=1),
        strategy_id=strategy.declaration.strategy_id,
        state_schema=strategy.declaration.state_schema,
        core=_closing_session_with_nothing_to_end_it(),
        strategy=strategy.state,
        event_cursor=EventCursor(sequence=0),
    )
    payload = RuntimeRecord[CounterState].fresh(envelope).model_dump_json().encode()
    store.commit(None, StoredEnvelope(revision=0, schema_version=1, payload=payload), fence, now=0)
    shell = CoreRuntime(store, CounterStrategy(), initial_state())
    with pytest.raises(OrphanWaitError):
        shell.start("reader", now_at=1, lease_duration=1)
    with pytest.raises(RuntimeCommitError):
        shell.decide(now_at=2)


class _ReleaseFails(FakeStateStore):
    """A store whose lease release raises ``error``, as a corrupt lease document does."""

    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    def release(self, fence: StoreFence, now: float) -> bool:
        del fence, now
        raise self._error


def _validation_error() -> ValidationError:
    try:
        StoredEnvelope.model_validate_json("{not json")
    except ValidationError as error:
        return error
    message = "invalid JSON validated"
    raise AssertionError(message)


@pytest.mark.parametrize(
    "error",
    [
        OSError("disk gone"),
        ValueError("corrupt lease"),
        _validation_error(),
        RuntimeError("store closed"),
        KeyError("fence"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_a_failed_lease_release_never_raises_and_ends_the_shell(error: Exception) -> None:
    shell = runtime(_ReleaseFails(error))
    shell.start("host", now_at=0, lease_duration=10)

    shell.release_lease(now_at=1)

    with pytest.raises(RuntimeCommitError):
        shell.decide(now_at=2)
