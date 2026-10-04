"""Crash boundaries use a real FakeStateStore and the public kernel trace seam."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.runtime_core_shell import CounterState, CounterStrategy, ShellTraceTransitions

from vs_core.api import (
    ClockAdvanced,
    ContractError,
    CoreEvent,
    CoreState,
    HostFence,
    HostId,
    IntentPhase,
    OperationRegistry,
    Transition,
    initial_state,
)
from vs_project.api import Committed, FakeStateStore, StoredEnvelope, Unknown
from vs_runtime.api.core import (
    CoreRuntime,
    CoreRuntimeBindings,
    DispatchProgress,
    ExecutorRefusal,
    ExecutorRole,
    RequestExecutors,
    RuntimeCommitUncertainError,
    RuntimeRecord,
)
from vs_runtime.api.testing import FakeRequestExecution

if TYPE_CHECKING:
    from vs_project.api import CommitOutcome, StoreFence


class CrashPoint(StrEnum):
    AFTER_STEP = "after_step"
    AFTER_COMMIT = "after_commit"
    AFTER_DISPATCH = "after_dispatch"


class StepCrashError(RuntimeError):
    pass


class CrashAfterStepTransitions(ShellTraceTransitions):
    def step(self, state: CoreState, event: CoreEvent) -> Transition:
        transition = super().step(state, event)
        if isinstance(event, ClockAdvanced):
            message = "crash between pure step and storage commit"
            raise StepCrashError(message)
        return transition


class LostAcknowledgementStateStore(FakeStateStore):
    """Actual whole-record commit, then deterministic acknowledgement loss."""

    def __init__(self, unknown_revision: int) -> None:
        super().__init__()
        self.unknown_revision = unknown_revision

    def commit(
        self, expected_revision: int | None, envelope: StoredEnvelope, fence: StoreFence, now: float
    ) -> CommitOutcome:
        result = super().commit(expected_revision, envelope, fence, now)
        if isinstance(result, Committed) and envelope.revision == self.unknown_revision:
            return Unknown(revision=envelope.revision)
        return result


def shell_with_requests(
    store: FakeStateStore, executor: FakeRequestExecution, *, crash_step: bool = False
) -> CoreRuntime[CounterState]:

    transitions = (
        CrashAfterStepTransitions(with_requests=True)
        if crash_step
        else ShellTraceTransitions(with_requests=True)
    )
    return CoreRuntime(
        store,
        CounterStrategy(),
        initial_state(),
        bindings=CoreRuntimeBindings(
            transitions=transitions,
            executors=RequestExecutors(
                sessions=executor, operations=FakeRequestExecution(ExecutorRole.OPERATIONS)
            ),
        ),
    )


@pytest.mark.asyncio
@given(
    times=st.lists(st.integers(min_value=1, max_value=20), unique=True, min_size=1, max_size=8),
    crash=st.sampled_from(list(CrashPoint)),
)
async def test_generated_crash_points_preserve_exact_committed_envelopes_and_no_duplicate_dispatch(
    times: list[int], crash: CrashPoint
) -> None:
    store = FakeStateStore()
    executor = FakeRequestExecution(ExecutorRole.SESSIONS)
    shell = shell_with_requests(store, executor, crash_step=crash == CrashPoint.AFTER_STEP)
    shell.start("first", now_at=0, lease_duration=100)
    for time in sorted(times):
        shell.submit(ClockAdvanced(now_at=time), now_at=time)
        if crash == CrashPoint.AFTER_STEP:
            before = store.load()
            with pytest.raises(StepCrashError):
                shell.advance()
            assert store.load() == before
            break
        assert shell.advance()
        stored = store.load()
        assert isinstance(stored, StoredEnvelope)
        assert RuntimeRecord[CounterState].decode(stored, OperationRegistry()) == shell.record
        if crash == CrashPoint.AFTER_DISPATCH:
            assert await shell.dispatch_one(now_at=time) == DispatchProgress.DISPATCHED
            # Drop the process-local completion queue. The next host must inspect,
            # rather than replay the already authorized original request.
            break
        assert executor.executions == ()
    stored = store.load()
    assert isinstance(stored, StoredEnvelope)
    exact = RuntimeRecord[CounterState].decode(stored, OperationRegistry())
    assert exact == shell.record
    restarted = shell_with_requests(store, executor)
    restarted.start("second", now_at=100, lease_duration=100)
    if crash == CrashPoint.AFTER_STEP:
        for time in sorted(times):
            restarted.submit(ClockAdvanced(now_at=time), now_at=100)
            restarted.advance()
    while True:
        outcome = await restarted.dispatch_one(now_at=100)
        if outcome == DispatchProgress.IDLE:
            break
        assert outcome == DispatchProgress.DISPATCHED
        while restarted.advance():
            pass
    identities = [row.request.request_id for row in executor.executions]
    assert len(identities) == len(set(identities))
    if crash == CrashPoint.AFTER_DISPATCH:
        assert len(executor.executions) == 1
        assert any(
            row.phase == IntentPhase.DISPATCHED
            for row in restarted.record.envelope.core.intents.intents
            if row.request.kind == "ensure_session"
        )
    else:
        assert len(executor.executions) == len(times)


@pytest.mark.asyncio
async def test_unknown_dispatch_authorization_never_calls_executor() -> None:
    store = LostAcknowledgementStateStore(unknown_revision=2)
    executor = FakeRequestExecution(ExecutorRole.SESSIONS)
    shell = shell_with_requests(store, executor)
    shell.start("host", now_at=0, lease_duration=100)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    with pytest.raises(RuntimeCommitUncertainError) as error:
        await shell.dispatch_one(now_at=1)
    assert error.value.candidate_visible
    assert executor.executions == ()
    assert shell.record.envelope.core.intents.intents[0].phase == IntentPhase.DISPATCHED


@pytest.mark.asyncio
async def test_unbound_executor_returns_named_typed_refusal() -> None:

    store = FakeStateStore()
    shell = CoreRuntime(
        store,
        CounterStrategy(),
        initial_state(),
        bindings=CoreRuntimeBindings(transitions=ShellTraceTransitions(with_requests=True)),
    )
    shell.start("host", now_at=0, lease_duration=100)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    refusal = await shell.dispatch_one(now_at=1)
    assert isinstance(refusal, ExecutorRefusal)
    assert refusal.role == ExecutorRole.SESSIONS
    assert "executor not bound" in refusal.detail


@pytest.mark.asyncio
async def test_fake_executor_identity_conflicts_and_old_epochs_are_rejected() -> None:
    store = FakeStateStore()
    executor = FakeRequestExecution(ExecutorRole.SESSIONS)
    shell = shell_with_requests(store, executor)
    shell.start("first", now_at=0, lease_duration=100)
    shell.submit(ClockAdvanced(now_at=1), now_at=1)
    shell.advance()
    assert await shell.dispatch_one(now_at=1) == DispatchProgress.DISPATCHED
    row = executor.executions[0]
    newer = row.context.model_copy(
        update={"fence": HostFence(host_id=HostId(root="second"), epoch=2)}
    )
    assert await executor.execute(row.request, newer) == row.result
    assert len(executor.executions) == 1
    with pytest.raises(ContractError, match="stale"):
        await executor.execute(row.request, row.context)
    with pytest.raises(ContractError, match="payload"):
        await executor.execute(row.request.model_copy(update={"deadline_at": 99.0}), newer)
