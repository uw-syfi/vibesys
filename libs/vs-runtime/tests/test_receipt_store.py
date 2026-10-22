"""ReceiptStore.run_once: at most one effect per request identity, with authority and resume."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict

from vs_core.api import BlockRequest, FailureReason, RequestId
from vs_project.api import Project, ProjectStateError
from vs_runtime.api.core import (
    BegunUnsealed,
    Conflict,
    Declined,
    NeverBegun,
    Performed,
    ReceiptCorruptError,
    ReceiptStore,
    Replayed,
    SealedExecution,
    Settled,
    Transient,
    result_type_name,
)
from vs_runtime.api.executor_contexts import RevocableLease, context_for
from vs_runtime.api.operation_fixtures import SCOPE
from vs_sim.api.testing import Gate, SimThreads, examples

if TYPE_CHECKING:
    from vs_runtime.api.core import ExecutionContext

pytestmark = pytest.mark.asyncio


class Done(BaseModel):
    """A sealed effect result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    value: int


def request(name: str = "r1") -> BlockRequest:
    return BlockRequest(
        request_id=RequestId(root=name),
        scope=SCOPE,
        target=RequestId(root="t"),
        reason=FailureReason.LOST,
    )


class Performer:
    """A counted effect that settles, stays transient, or is told it resumed."""

    def __init__(self, *, transient_first: bool = False) -> None:
        self.calls = 0
        self.resumed: list[bool] = []
        self._transient_first = transient_first

    async def __call__(self, *, resumed: bool) -> Settled[Done] | Transient[Done]:
        self.calls += 1
        self.resumed.append(resumed)
        if self._transient_first and self.calls == 1:
            return Transient(Done(value=0))
        return Settled(Done(value=self.calls))


class HeldPerformer:
    """A counted effect whose completion the test controls."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = Gate()
        self.release = Gate()

    async def __call__(self, *, resumed: bool) -> Settled[Done]:
        del resumed
        self.calls += 1
        self.started.open()
        await self.release.wait()
        return Settled(Done(value=1))


class HeldTransientPerformer:
    """A transient effect whose completion the test controls."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = Gate()
        self.release = Gate()

    async def __call__(self, *, resumed: bool) -> Transient[Done]:
        del resumed
        self.calls += 1
        self.started.open()
        await self.release.wait()
        return Transient(Done(value=1))


class PerformerError(RuntimeError):
    """An ordinary performer failure the receipt boundary must propagate."""

    def __init__(self) -> None:
        super().__init__("performer failed")


class HeldFailingPerformer:
    """A failing effect whose exception the test releases after a follower queues."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = Gate()
        self.release = Gate()

    async def __call__(self, *, resumed: bool) -> Settled[Done]:
        del resumed
        self.calls += 1
        self.started.open()
        await self.release.wait()
        raise PerformerError


async def run(
    store: ReceiptStore,
    effect: Performer | HeldPerformer | HeldTransientPerformer | HeldFailingPerformer,
    context: ExecutionContext,
    key: str = "r1",
) -> object:
    return await store.run_once(key, owner="o", context=context, result_type=Done, perform=effect)


async def scheduled_run(
    store: ReceiptStore,
    effect: Performer | HeldPerformer | HeldTransientPerformer | HeldFailingPerformer,
    context: ExecutionContext,
    scheduled: Gate,
    key: str = "r1",
) -> object:
    """Announce delivery before entering the public receipt boundary."""
    scheduled.open()
    return await run(store, effect, context, key)


def store_at(root: Path) -> ReceiptStore:
    (root / "project").mkdir(exist_ok=True)
    return ReceiptStore(
        Project.open(root / "project").state.state_store_namespace("run"), threads=SimThreads()
    )


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
    max_examples=examples(pr=5, nightly=25),
)
@given(repeats=st.integers(min_value=1, max_value=5))
async def test_a_settled_effect_runs_once_however_often_the_request_is_replayed(
    repeats: int,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        effect = Performer()
        ctx = context_for(request())
        first = await run(store_at(Path(raw)), effect, ctx)
        assert isinstance(first, Performed)
        for _ in range(repeats):
            again = await run(store_at(Path(raw)), effect, ctx)
            assert again == Replayed(first.result)
        assert effect.calls == 1


async def test_a_live_duplicate_waits_for_and_replays_the_in_flight_result() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        effect = HeldPerformer()
        context = context_for(request())
        first = asyncio.create_task(run(store, effect, context))
        await effect.started.wait(first)
        scheduled = Gate()
        duplicate = asyncio.create_task(scheduled_run(store, effect, context, scheduled))
        await scheduled.wait(duplicate)

        assert effect.calls == 1
        effect.release.open()
        performed, shared = await asyncio.gather(first, duplicate)

        assert performed == Performed(Done(value=1))
        assert shared is performed
        assert await run(store, Performer(), context) == Replayed(Done(value=1))


@settings(deadline=None, max_examples=examples(pr=5, nightly=20))
@given(duplicates=st.integers(min_value=1, max_value=5))
async def test_any_number_of_live_duplicates_share_one_performer(duplicates: int) -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        effect = HeldPerformer()
        context = context_for(request())
        first = asyncio.create_task(run(store, effect, context))
        await effect.started.wait(first)
        scheduled = [Gate() for _ in range(duplicates)]
        followers = [
            asyncio.create_task(scheduled_run(store, effect, context, gate)) for gate in scheduled
        ]
        for gate, follower in zip(scheduled, followers, strict=True):
            await gate.wait(follower)

        assert effect.calls == 1
        effect.release.open()
        results = await asyncio.gather(first, *followers)

        assert results == [Performed(Done(value=1)) for _ in range(duplicates + 1)]
        assert all(result is results[0] for result in results)


async def test_live_duplicates_share_a_transient_outcome_before_a_later_retry() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        effect = HeldTransientPerformer()
        context = context_for(request())
        first = asyncio.create_task(run(store, effect, context))
        await effect.started.wait(first)
        scheduled = Gate()
        duplicate = asyncio.create_task(scheduled_run(store, effect, context, scheduled))
        await scheduled.wait(duplicate)

        effect.release.open()
        original, shared = await asyncio.gather(first, duplicate)

        assert original == Performed(Done(value=1))
        assert shared is original
        assert effect.calls == 1

        retry = Performer()
        assert await run(store, retry, context) == Performed(Done(value=1))
        assert retry.resumed == [True]


async def test_a_conflicting_live_duplicate_never_enters_the_performer() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        effect = HeldPerformer()
        first_context = context_for(request())
        conflicting = context_for(request().model_copy(update={"reason": FailureReason.OWNER_GONE}))
        first = asyncio.create_task(run(store, effect, first_context))
        await effect.started.wait(first)
        scheduled = Gate()
        duplicate = asyncio.create_task(scheduled_run(store, effect, conflicting, scheduled))
        await scheduled.wait(duplicate)

        assert effect.calls == 1
        effect.release.open()
        performed, rejected = await asyncio.gather(first, duplicate)

        assert performed == Performed(Done(value=1))
        assert rejected == Conflict()


async def test_canceling_a_live_duplicate_does_not_cancel_the_performer() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        effect = HeldPerformer()
        context = context_for(request())
        first = asyncio.create_task(run(store, effect, context))
        await effect.started.wait(first)
        scheduled = Gate()
        duplicate = asyncio.create_task(scheduled_run(store, effect, context, scheduled))
        await scheduled.wait(duplicate)

        duplicate.cancel()
        with pytest.raises(asyncio.CancelledError):
            await duplicate
        assert effect.calls == 1

        effect.release.open()
        assert await first == Performed(Done(value=1))


async def test_canceling_the_active_delivery_is_shared_with_its_queued_follower() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        context = context_for(request())
        leader_effect = HeldPerformer()
        leader = asyncio.create_task(run(store, leader_effect, context))
        await leader_effect.started.wait(leader)
        scheduled = Gate()
        follower = asyncio.create_task(scheduled_run(store, leader_effect, context, scheduled))
        await scheduled.wait(follower)
        assert not follower.done()

        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        leader_effect.release.open()
        with pytest.raises(asyncio.CancelledError):
            await follower
        assert leader_effect.calls == 1

        retry = Performer()
        assert await run(store, retry, context) == Performed(Done(value=1))
        assert retry.resumed == [True]


async def test_a_performer_exception_is_shared_with_its_queued_follower() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        context = context_for(request())
        leader_effect = HeldFailingPerformer()
        leader = asyncio.create_task(run(store, leader_effect, context))
        await leader_effect.started.wait(leader)
        scheduled = Gate()
        follower = asyncio.create_task(scheduled_run(store, leader_effect, context, scheduled))
        await scheduled.wait(follower)
        assert not follower.done()

        leader_effect.release.open()
        with pytest.raises(PerformerError, match="performer failed") as original:
            await leader
        with pytest.raises(PerformerError, match="performer failed") as shared:
            await follower
        assert shared.value is original.value
        assert leader_effect.calls == 1

        retry = Performer()
        assert await run(store, retry, context) == Performed(Done(value=1))
        assert retry.resumed == [True]


async def test_different_request_identities_remain_concurrent() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        effect = HeldPerformer()
        first = asyncio.create_task(run(store, effect, context_for(request("r1")), "r1"))
        await effect.started.wait(first)
        scheduled = Gate()
        second = asyncio.create_task(
            scheduled_run(store, effect, context_for(request("r2")), scheduled, "r2")
        )
        await scheduled.wait(second)

        assert effect.calls == 2
        effect.release.open()
        assert await asyncio.gather(first, second) == [
            Performed(Done(value=1)),
            Performed(Done(value=1)),
        ]


async def test_an_in_flight_effect_reports_its_type_and_recorded_payload() -> None:
    """Unsealed history keeps the result type callers already read, plus that attempt's digest."""
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        assert isinstance(store.history("r1"), NeverBegun)
        effect = HeldPerformer()
        context = context_for(request())
        first = asyncio.create_task(run(store, effect, context))
        await effect.started.wait(first)
        history = store.history("r1")
        assert isinstance(history, BegunUnsealed)
        assert history.result_type == result_type_name(Done)
        assert history.payload_digest == context.payload_digest
        effect.release.open()
        assert isinstance(await first, Performed)
        sealed = store.history("r1")
        assert isinstance(sealed, SealedExecution)
        assert sealed.result_type == history.result_type
        assert sealed.payload_digest == history.payload_digest


async def test_a_transient_result_is_not_sealed_and_the_retry_is_told_it_resumes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        effect = Performer(transient_first=True)
        ctx = context_for(request())
        assert isinstance(await run(store_at(Path(raw)), effect, ctx), Performed)
        assert isinstance(await run(store_at(Path(raw)), effect, ctx), Performed)
        assert isinstance(await run(store_at(Path(raw)), effect, ctx), Replayed)
        assert effect.resumed == [False, True]


async def test_another_payload_under_the_same_identity_conflicts_without_an_effect() -> None:
    with tempfile.TemporaryDirectory() as raw:
        effect = Performer()
        store = store_at(Path(raw))
        await run(store, effect, context_for(request()))
        other = context_for(request().model_copy(update={"reason": FailureReason.OWNER_GONE}))
        assert await run(store, effect, other) == Conflict()
        assert effect.calls == 1


async def test_a_lost_lease_or_stale_fence_runs_no_effect_and_leaves_no_marker() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        lost = RevocableLease()
        lost.valid = False
        effect = Performer()
        assert isinstance(await run(store, effect, context_for(request(), lease=lost)), Declined)
        newer = Performer()
        await run(store, newer, context_for(request("r2"), epoch=3), "r2")
        for ctx in (
            context_for(request(), epoch=2),
            context_for(request(), epoch=3, host="another"),
        ):
            assert isinstance(await run(store, effect, ctx), Declined)
        assert effect.calls == 0
        # The refused request was never begun, so a holder of authority runs it fresh.
        assert isinstance(await run(store, effect, context_for(request(), epoch=3)), Performed)
        assert effect.resumed == [False]


async def test_authority_lost_during_the_effect_does_not_seal_the_result() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        lease = RevocableLease()

        async def revoking(*, resumed: bool) -> Settled[Done]:
            del resumed
            lease.valid = False
            return Settled(Done(value=1))

        ctx = context_for(request(), lease=lease)
        got = await store.run_once("r1", owner="o", context=ctx, result_type=Done, perform=revoking)
        assert isinstance(got, Declined)
        effect = Performer()
        assert isinstance(await run(store, effect, context_for(request())), Performed)
        assert effect.resumed == [True]


async def test_an_unreadable_receipt_is_refused_not_guessed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        effect = Performer()
        await run(store, effect, context_for(request()))
        for path in (Path(raw) / "project").rglob("*.execution.json"):
            path.write_text("{not json")
        got = await run(store_at(Path(raw)), effect, context_for(request()))
        assert isinstance(got, Declined)
        assert effect.calls == 1


async def test_only_a_damaged_receipt_is_corruption_an_invalid_path_is_not() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        await run(store, Performer(), context_for(request()))
        for path in (Path(raw) / "project").rglob("*.execution.json"):
            path.write_text("secret-receipt-bytes")
        with pytest.raises(ReceiptCorruptError) as damaged:
            store.history("r1")
        assert "secret-receipt-bytes" not in str(damaged.value.__cause__)
        # A family that is not a safe path is a caller bug, not a damaged receipt.
        with pytest.raises(ProjectStateError) as invalid:
            store.load("../escape", "execution", "r1", Done)
        assert not isinstance(invalid.value, ReceiptCorruptError)
