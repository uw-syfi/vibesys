"""ReceiptStore.run_once: at most one effect per request identity, with authority and resume."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel, ConfigDict
from tests.support.executor_context import RevocableLease, context_for
from tests.support.runtime_operations import SCOPE

from vs_core.api import BlockIntent, RequestId
from vs_project.api import Project
from vs_runtime.api.core import (
    Conflict,
    Performed,
    ReceiptStore,
    Refused,
    Replayed,
    Settled,
    Transient,
)

if TYPE_CHECKING:
    from vs_runtime.api.core import ExecutionContext

pytestmark = pytest.mark.asyncio


class Done(BaseModel):
    """A sealed effect result."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    value: int


def request(name: str = "r1") -> BlockIntent:
    return BlockIntent(
        request_id=RequestId(root=name),
        scope=SCOPE,
        deadline_at=100.0,
        target=RequestId(root="t"),
        diagnostic="d",
    )


class Effect:
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


async def run(
    store: ReceiptStore, effect: Effect, context: ExecutionContext, key: str = "r1"
) -> object:
    return await store.run_once(key, owner="o", context=context, result_type=Done, perform=effect)


def store_at(root: Path) -> ReceiptStore:
    (root / "project").mkdir(exist_ok=True)
    return ReceiptStore(Project.open(root / "project").state.state_store_namespace("run"))


@settings(
    suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None, max_examples=25
)
@given(repeats=st.integers(min_value=1, max_value=5))
async def test_a_settled_effect_runs_once_however_often_the_request_is_replayed(
    repeats: int,
) -> None:
    with tempfile.TemporaryDirectory() as raw:
        effect = Effect()
        ctx = context_for(request())
        first = await run(store_at(Path(raw)), effect, ctx)
        assert isinstance(first, Performed)
        for _ in range(repeats):
            again = await run(store_at(Path(raw)), effect, ctx)
            assert again == Replayed(first.result)
        assert effect.calls == 1


async def test_a_transient_result_is_not_sealed_and_the_retry_is_told_it_resumes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        effect = Effect(transient_first=True)
        ctx = context_for(request())
        assert isinstance(await run(store_at(Path(raw)), effect, ctx), Performed)
        assert isinstance(await run(store_at(Path(raw)), effect, ctx), Performed)
        assert isinstance(await run(store_at(Path(raw)), effect, ctx), Replayed)
        assert effect.resumed == [False, True]


async def test_another_payload_under_the_same_identity_conflicts_without_an_effect() -> None:
    with tempfile.TemporaryDirectory() as raw:
        effect = Effect()
        store = store_at(Path(raw))
        await run(store, effect, context_for(request()))
        other = context_for(request().model_copy(update={"diagnostic": "other"}))
        assert await run(store, effect, other) == Conflict()
        assert effect.calls == 1


async def test_a_lost_lease_or_stale_fence_runs_no_effect_and_leaves_no_marker() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        lost = RevocableLease()
        lost.valid = False
        effect = Effect()
        assert isinstance(await run(store, effect, context_for(request(), lease=lost)), Refused)
        newer = Effect()
        await run(store, newer, context_for(request("r2"), epoch=3), "r2")
        for ctx in (
            context_for(request(), epoch=2),
            context_for(request(), epoch=3, host="another"),
        ):
            assert isinstance(await run(store, effect, ctx), Refused)
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
        assert isinstance(got, Refused)
        effect = Effect()
        assert isinstance(await run(store, effect, context_for(request())), Performed)
        assert effect.resumed == [True]


async def test_an_unreadable_receipt_is_refused_not_guessed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        store = store_at(Path(raw))
        effect = Effect()
        await run(store, effect, context_for(request()))
        for path in (Path(raw) / "project").rglob("*.execution.json"):
            path.write_text("{not json")
        got = await run(store_at(Path(raw)), effect, context_for(request()))
        assert isinstance(got, Refused)
        assert effect.calls == 1
