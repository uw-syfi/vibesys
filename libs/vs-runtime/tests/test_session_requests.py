"""EnsureSession, DispatchTurn and InspectTurn over durable sessions and the Fake agent client."""

from __future__ import annotations

import asyncio
import tempfile
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.executor_context import RevocableLease
from tests.support.observation_contract import assert_core_accepts
from tests.support.session_world import (
    CrashOnReplace,
    SessionHost,
    dispatch_request,
    ensure_request,
    inspect_request,
    open_host,
    reuse_ensure,
    run_snapshot,
    turn_output,
)

from vs_core.api import (
    ObservationStatus,
    ResourceId,
)
from vs_project.api import Project
from vs_runtime.api.core import ExecutionResult, JournalRunInvocations, ReceiptStore

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from vs_core.api import RequestBase


class World:
    """One Project directory and host; ``execute`` is a host (re)start over the same disk."""

    def __init__(self, base: Path) -> None:
        (base / "project").mkdir()
        (base / "workspace").mkdir()
        self._project = Project.open(base / "project")
        self.host: SessionHost = open_host(base / "workspace")

    def store(self, *, dies_before_seal: bool = False) -> ReceiptStore:
        namespace = self._project.state.state_store_namespace("run")
        return CrashOnReplace(namespace) if dies_before_seal else ReceiptStore(namespace)

    async def execute(
        self,
        request: RequestBase,
        *,
        lease: RevocableLease | None = None,
        now_at: float | None = None,
        dies_before_seal: bool = False,
    ) -> ExecutionResult:
        return await self.host.run(
            request, self.store(dies_before_seal=dies_before_seal), lease=lease, now_at=now_at
        )


@asynccontextmanager
async def world() -> AsyncIterator[World]:
    with tempfile.TemporaryDirectory() as raw:
        yield World(Path(raw))


def status(result: ExecutionResult) -> ObservationStatus:
    return result.observation.observation.status


@pytest.mark.asyncio
async def test_ensure_binds_one_resource_and_replays_it() -> None:
    async with world() as w:
        first = await w.execute(ensure_request())
        again = await w.execute(ensure_request())
        assert status(first) is ObservationStatus.SUCCEEDED
        assert first.observation.observation.accepted
        assert first.observation.observation.children_complete
        assert again == first
        assert first.observation.observation.resource_id is not None


@pytest.mark.asyncio
async def test_reuse_without_a_binding_is_rejected_and_never_creates_a_fresh_session() -> None:
    async with world() as w:
        missing = reuse_ensure("req-reuse", ResourceId(root="session:gone"))
        result = await w.execute(missing)
        assert status(result) is ObservationStatus.REJECTED
        assert result.observation.observation.terminal
        # A later fresh ensure of the same identity is the first binding, not a hidden second one.
        fresh = await w.execute(ensure_request("req-fresh"))
        assert status(fresh) is ObservationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_reuse_reattaches_the_same_resource_across_a_restart() -> None:
    async with world() as w:
        first = await w.execute(ensure_request())
        resource = first.observation.observation.resource_id
        await w.execute(dispatch_request())
        again = await w.execute(reuse_ensure("req-reattach", resource))
        assert status(again) is ObservationStatus.SUCCEEDED
        assert again.observation.observation.resource_id == resource
        other = await w.execute(reuse_ensure("req-wrong", ResourceId(root="session:other")))
        assert status(other) is ObservationStatus.REJECTED


@pytest.mark.asyncio
async def test_a_lost_provider_conversation_is_not_replaced_by_a_fresh_one() -> None:
    async with world() as w:
        first = await w.execute(ensure_request())
        await w.execute(dispatch_request())
        w.host = w.host.restart_lost_conversation(w.host.resolver.workspace)
        result = await w.execute(
            reuse_ensure("req-reattach", first.observation.observation.resource_id)
        )
        assert status(result) is ObservationStatus.REJECTED
        follow_up = await w.execute(dispatch_request("req-two", "inv-2"))
        assert status(follow_up) is ObservationStatus.REJECTED
        assert w.host.turns == []


@pytest.mark.asyncio
async def test_fresh_policy_cannot_take_an_identity_another_request_bound() -> None:
    async with world() as w:
        await w.execute(ensure_request("req-a"))
        result = await w.execute(ensure_request("req-b"))
        assert status(result) is ObservationStatus.REJECTED


@pytest.mark.asyncio
async def test_undeclared_role_is_rejected() -> None:
    async with world() as w:
        w.host.resolver.roles = frozenset()
        assert status(await w.execute(ensure_request())) is ObservationStatus.REJECTED


@pytest.mark.asyncio
async def test_dispatch_completes_with_validated_output_and_continues_the_conversation() -> None:
    async with world() as w:
        await w.execute(ensure_request())
        first = await w.execute(dispatch_request())
        second = await w.execute(dispatch_request("req-two", "inv-2"))
        assert status(first) is status(second) is ObservationStatus.SUCCEEDED
        event = first.owner_events[0]
        assert event.kind == "turn_observed"
        assert event.output_json == '{"value":7}'
        assert_core_accepts([first, second], expect_retry=False)
        assert len(w.host.turns) == 2
        assert w.host.turns[0].expected_provider_session_id is None
        assert w.host.turns[1].expected_provider_session_id is not None


@pytest.mark.asyncio
async def test_dispatch_needs_an_ensured_session() -> None:
    async with world() as w:
        result = await w.execute(dispatch_request())
        assert status(result) is ObservationStatus.REJECTED
        assert w.host.turns == []


@pytest.mark.parametrize("problem", ["schema", "message"])
@pytest.mark.asyncio
async def test_an_unresolvable_turn_is_rejected_before_any_provider_turn(problem: str) -> None:
    async with world() as w:
        await w.execute(ensure_request())
        if problem == "schema":
            w.host.resolver.schemas.clear()
        else:
            w.host.resolver.messages_resolve = False
        result = await w.execute(dispatch_request())
        assert status(result) is ObservationStatus.REJECTED
        assert w.host.turns == []


@pytest.mark.asyncio
async def test_a_turn_past_its_deadline_is_rejected_without_dispatch() -> None:
    async with world() as w:
        await w.execute(ensure_request())
        result = await w.execute(dispatch_request(), now_at=500.0)
        assert status(result) is ObservationStatus.REJECTED
        assert w.host.turns == []


@pytest.mark.asyncio
async def test_a_late_retry_after_a_crash_replays_the_turn_that_already_ran() -> None:
    async with world() as w:
        await w.execute(ensure_request())
        with pytest.raises(SystemExit):
            await w.execute(dispatch_request(), dies_before_seal=True)
        assert len(w.host.turns) == 1
        retried = await w.execute(dispatch_request(), now_at=500.0)
        inspected = await w.execute(inspect_request(), now_at=500.0)
        assert status(retried) is ObservationStatus.SUCCEEDED
        assert turn_output(retried) == '{"value":7}'
        assert inspected.observation.target is not None
        assert inspected.observation.target.observation.status is ObservationStatus.SUCCEEDED
        assert len(w.host.turns) == 1
        assert_core_accepts([retried, inspected], expect_retry=False)


@pytest.mark.asyncio
async def test_a_late_retry_of_a_turn_that_may_have_started_is_unknown_never_rejected() -> None:
    async with world() as w:
        await w.execute(ensure_request())
        w.host.faults.down = True
        await w.execute(dispatch_request())
        retried = await w.execute(dispatch_request(), now_at=500.0)
        assert status(retried) is ObservationStatus.UNKNOWN
        assert not retried.observation.observation.terminal
        assert len(w.host.turns) == 1


@pytest.mark.asyncio
async def test_a_crash_right_after_a_turn_cannot_skip_the_lost_checkpoint_guard() -> None:
    async with world() as w:
        first = await w.execute(ensure_request())
        with pytest.raises(SystemExit):
            await w.execute(dispatch_request(), dies_before_seal=True)
        assert len(w.host.turns) == 1
        w.host = w.host.restart_lost_conversation(w.host.resolver.workspace)
        follow_up = await w.execute(dispatch_request("req-two", "inv-2"))
        assert status(follow_up) is ObservationStatus.REJECTED
        assert w.host.turns == []  # no fresh conversation took the lost one's place
        again = await w.execute(
            reuse_ensure("req-reattach", first.observation.observation.resource_id)
        )
        assert status(again) is ObservationStatus.REJECTED


@pytest.mark.asyncio
async def test_a_terminal_turn_is_released_so_a_closing_attempt_can_drain() -> None:
    async with world() as w:
        ensured = await w.execute(ensure_request())
        done = await w.execute(dispatch_request())
        seen = await w.execute(inspect_request())
        assert not ensured.observation.observation.released
        assert done.observation.observation.terminal
        assert done.observation.observation.released
        assert done.observation.observation.children_complete
        assert seen.observation.target is not None
        assert seen.observation.target.observation.released
        w.host.faults.down = True
        lost = await w.execute(dispatch_request("req-two", "inv-2"))
        assert not lost.observation.observation.terminal
        assert not lost.observation.observation.released


@pytest.mark.asyncio
async def test_the_in_turn_timeout_is_the_one_bound_with_the_session() -> None:
    async with world() as w:
        await w.execute(ensure_request())
        await w.execute(dispatch_request())
        await w.execute(dispatch_request("req-two", "inv-2"))
        assert [turn.timeout for turn in w.host.turns] == [timedelta(seconds=30)] * 2
        w.host.resolver.timeout = timedelta(seconds=60)
        changed = await w.execute(dispatch_request("req-three", "inv-3"))
        assert status(changed) is ObservationStatus.REJECTED
        assert "timeout" in changed.observation.observation.diagnostic
        assert len(w.host.turns) == 2


@pytest.mark.parametrize("seconds", [0.0, -1.0])
@pytest.mark.asyncio
async def test_an_invalid_turn_timeout_is_rejected_when_the_session_is_bound(
    seconds: float,
) -> None:
    async with world() as w:
        w.host.resolver.timeout = timedelta(seconds=seconds)
        result = await w.execute(ensure_request())
        assert status(result) is ObservationStatus.REJECTED
        assert "timeout" in result.observation.observation.diagnostic


@pytest.mark.asyncio
async def test_unknown_acceptance_is_inspected_and_never_dispatched_again() -> None:
    async with world() as w:
        await w.execute(ensure_request())
        w.host.faults.down = True
        lost = await w.execute(dispatch_request())
        assert status(lost) is ObservationStatus.UNKNOWN
        assert not lost.observation.observation.terminal
        w.host.faults.down = False
        retried = await w.execute(dispatch_request())
        inspected = await w.execute(inspect_request())
        assert status(retried) is ObservationStatus.UNKNOWN
        assert inspected.observation.target is not None
        assert inspected.observation.target.observation.status is ObservationStatus.UNKNOWN
        assert len(w.host.turns) == 1
        assert_core_accepts([lost, retried, inspected], expect_retry=False)


@pytest.mark.asyncio
async def test_inspect_translates_a_completed_turn_and_a_never_dispatched_one() -> None:
    async with world() as w:
        await w.execute(ensure_request())
        never = await w.execute(inspect_request("req-i1", "inv-1"))
        assert never.observation.target is not None
        target = never.observation.target.observation
        assert target.status is ObservationStatus.FAILED
        assert target.terminal
        assert not target.accepted
        dispatched = await w.execute(dispatch_request())
        seen = await w.execute(inspect_request("req-i2", "inv-1"))
        assert seen.observation.target is not None
        assert seen.observation.target.observation.status is ObservationStatus.SUCCEEDED
        assert turn_output(seen) == turn_output(dispatched) is not None
        assert_core_accepts([dispatched, seen], expect_retry=False)


@pytest.mark.asyncio
async def test_one_invocation_cannot_be_dispatched_by_two_requests() -> None:
    async with world() as w:
        await w.execute(ensure_request())
        await w.execute(dispatch_request("req-a"))
        other = await w.execute(dispatch_request("req-b"))
        assert status(other) is ObservationStatus.REJECTED
        assert len(w.host.turns) == 1


@pytest.mark.asyncio
async def test_only_a_settled_turn_proves_the_run_writer_ended() -> None:
    async with world() as w:
        assert (
            JournalRunInvocations(w.host.sessions(), w.store()).unproven(run_snapshot()) is not None
        )
        await w.execute(ensure_request())
        await w.execute(dispatch_request("req-two", "inv-2"))
        w.host.faults.down = True
        await w.execute(dispatch_request("req-one", "inv-1"))
        proof = JournalRunInvocations(w.host.sessions(), w.store())
        assert proof.unproven(run_snapshot("inv-2")) is None
        assert proof.unproven(run_snapshot("inv-1")) is not None  # acceptance unknown
        assert proof.unproven(run_snapshot("inv-9")) is not None  # never dispatched


@pytest.mark.asyncio
async def test_stale_host_dispatches_nothing() -> None:
    async with world() as w:
        await w.execute(ensure_request())
        lost = RevocableLease()
        lost.valid = False
        result = await w.execute(dispatch_request(), lease=lost)
        assert status(result) is ObservationStatus.UNKNOWN
        assert w.host.turns == []


@settings(max_examples=25, deadline=None)
@given(
    steps=st.lists(
        st.tuples(st.sampled_from(["dispatch", "inspect", "restart-down"]), st.integers(0, 2)),
        min_size=1,
        max_size=12,
    )
)
def test_no_schedule_dispatches_an_invocation_to_the_provider_twice(
    steps: list[tuple[str, int]],
) -> None:
    async def run() -> None:
        async with world() as w:
            await w.execute(ensure_request())
            dispatched: set[int] = set()
            for action, number in steps:
                invocation = f"inv-{number}"
                if action == "dispatch":
                    await w.execute(dispatch_request(f"req-{number}", invocation))
                    dispatched.add(number)
                elif action == "inspect":
                    await w.execute(
                        inspect_request(f"req-i-{len(dispatched)}-{number}", invocation)
                    )
                else:
                    w.host.faults.down = not w.host.faults.down
                assert len(w.host.turns) <= len(dispatched)

    asyncio.run(run())
