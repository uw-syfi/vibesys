"""CloseOnce and AsyncCloseOnce end only on a Released proof, and re-run a failed close.

A Released proof is minted only from evidence, and a ReleaseStack keeps a failed step.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import (
    AsyncCloseOnce,
    CloseOnce,
    NothingLeft,
    NotReleasedError,
    Released,
    ReleaseStack,
)
from vs_sim.api.testing import VirtualClock, run_virtual

if TYPE_CHECKING:
    from collections.abc import Iterator


class _CloseFailedError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Shown:
    what: str
    released: bool = True

    def describe(self) -> str:
        return self.what


def _attempt(number: int) -> Released:
    return Released.shown_by(_Shown(f"attempt {number}"))


# Each entry is one close attempt's result: True releases, False fails.
_ATTEMPTS = st.lists(st.booleans(), min_size=1, max_size=6)


class _Effect:
    def __init__(self, results: list[bool]) -> None:
        self.results = list(results)
        self.runs = 0

    def __call__(self) -> Released:
        released = self.results[self.runs]
        self.runs += 1
        if not released:
            raise _CloseFailedError
        return _attempt(self.runs)

    async def run_async(self) -> Released:
        # test-isolation: virtual time under run_virtual; costs no wall time.
        await asyncio.sleep(1.0)
        return self()


def _expected_runs(results: list[bool], calls: int) -> int:
    """The effect runs once per call until the first release, then never again."""
    first = results.index(True) if True in results else len(results)
    return min(calls, first + 1)


@given(results=_ATTEMPTS)
def test_a_close_reruns_its_effect_until_it_returns_released(results: list[bool]) -> None:
    effect = _Effect(results)
    latch = CloseOnce()
    outcomes: list[Released | None] = []
    for _ in results:
        try:
            outcomes.append(latch.run(effect))
        except _CloseFailedError:
            outcomes.append(None)

    assert latch.started
    assert effect.runs == _expected_runs(results, len(results))
    if True in results:
        proof = _attempt(results.index(True) + 1)
        assert latch.released == proof
        assert outcomes[results.index(True) :] == [proof] * (len(results) - results.index(True))
    else:
        assert latch.released is None


@given(results=_ATTEMPTS)
def test_an_async_close_reruns_its_effect_until_it_returns_released(results: list[bool]) -> None:
    effect = _Effect(results)
    latch = AsyncCloseOnce()

    async def main() -> None:
        for _ in results:
            with suppress(_CloseFailedError):
                await latch.run(effect.run_async)

    run_virtual(VirtualClock(), main())

    assert latch.started
    assert effect.runs == _expected_runs(results, len(results))
    assert (latch.released is not None) is (True in results)


@pytest.mark.parametrize("first", ["released", "failed"])
def test_concurrent_async_closes_share_one_attempt(first: str) -> None:
    released = first == "released"
    effect = _Effect([released, True])
    latch = AsyncCloseOnce()

    async def main() -> list[Released | BaseException]:
        return [
            *await asyncio.gather(
                latch.run(effect.run_async), latch.run(effect.run_async), return_exceptions=True
            )
        ]

    outcomes = run_virtual(VirtualClock(), main())

    assert effect.runs == 1
    if released:
        assert outcomes == [_attempt(1)] * 2
    else:
        assert all(isinstance(outcome, _CloseFailedError) for outcome in outcomes)
        assert latch.released is None


def test_a_cancelled_caller_does_not_cancel_the_release() -> None:
    effect = _Effect([True])
    latch = AsyncCloseOnce()

    async def main() -> Released | None:
        caller = asyncio.create_task(latch.run(effect.run_async))
        # test-isolation: virtual time under run_virtual; costs no wall time.
        await asyncio.sleep(0.5)
        caller.cancel()
        # test-isolation: virtual time under run_virtual; costs no wall time.
        await asyncio.sleep(1.0)
        return latch.released

    assert run_virtual(VirtualClock(), main()) == _attempt(1)
    assert effect.runs == 1


def test_an_effect_that_returns_no_proof_is_refused() -> None:
    latch = CloseOnce()

    with pytest.raises(TypeError, match="must return Released"):
        latch.run(lambda: None)  # ty: ignore[invalid-argument-type]

    assert latch.released is None


def test_a_proof_is_minted_only_from_evidence_that_shows_a_release() -> None:
    with pytest.raises(TypeError, match="mint Released"):
        Released("the call returned")
    with pytest.raises(NotReleasedError, match="still here"):
        Released.shown_by(_Shown("still here", released=False))
    with pytest.raises(NotReleasedError, match="sessions: 1 left"):
        Released.shown_by(NothingLeft({"one"}, "sessions"))
    assert Released.shown_by(NothingLeft((), "sessions")).evidence == "sessions: 0 left"


# Per step, the results of its successive runs: True returns, False raises.
_STEP_RUNS = st.lists(st.lists(st.booleans(), min_size=1, max_size=4), min_size=1, max_size=5)


@given(steps=_STEP_RUNS)
def test_a_release_stack_reruns_each_failed_step_until_every_step_ran(
    steps: list[list[bool]],
) -> None:
    order: list[int] = []
    runs = [0] * len(steps)

    def step(index: int) -> None:
        result = steps[index][min(runs[index], len(steps[index]) - 1)]
        runs[index] += 1
        order.append(index)
        if not result:
            raise _CloseFailedError(index)

    stack = ReleaseStack()
    for index in range(len(steps)):
        stack.callback(step, index)
    closes = max(len(results) for results in steps) + 1
    proofs: list[Released | None] = []
    for _ in range(closes):
        before = len(order)
        try:
            proofs.append(stack.close())
        except _CloseFailedError:
            proofs.append(None)
        # Each close runs the steps still held, newest first.
        assert order[before:] == sorted(order[before:], reverse=True)

    for index, results in enumerate(steps):
        # A step runs until its first success, then never again.
        first = results.index(True) + 1 if True in results else closes
        assert runs[index] == first, (index, results)
    released = [proof is not None for proof in proofs]
    if all(True in results for results in steps):
        # Released from the first close at which every step had succeeded, and on every close after.
        settled = max(results.index(True) for results in steps)
        assert released == [i >= settled for i in range(closes)]
    else:
        assert not any(released)


def test_a_popped_release_stack_closes_nothing_and_the_new_one_closes_everything() -> None:
    ran: list[str] = []
    stack = ReleaseStack()
    stack.callback(ran.append, "first")
    stack.callback(ran.append, "second")
    moved = stack.pop_all()
    stack.close()
    assert ran == []
    moved.close()
    assert ran == ["second", "first"]


def test_a_context_whose_exit_failed_keeps_every_later_close_unreleased() -> None:
    exits: list[str] = []

    @contextmanager
    def lease() -> Iterator[None]:
        yield
        exits.append("exit")
        raise _CloseFailedError

    stack = ReleaseStack()
    stack.enter_context(lease())
    with pytest.raises(_CloseFailedError):
        stack.close()
    with pytest.raises(NotReleasedError, match="cannot be retried"):
        stack.close()
    assert exits == ["exit"]
