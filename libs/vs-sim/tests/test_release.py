"""CloseOnce and AsyncCloseOnce end only on a Released proof, and re-run a failed close."""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sim.api import AsyncCloseOnce, CloseOnce, Released
from vs_sim.api.testing import VirtualClock, run_virtual


class _CloseFailedError(RuntimeError):
    pass


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
        return Released(f"attempt {self.runs}")

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
        proof = Released(f"attempt {results.index(True) + 1}")
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
        assert outcomes == [Released("attempt 1")] * 2
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

    assert run_virtual(VirtualClock(), main()) == Released("attempt 1")
    assert effect.runs == 1


def test_an_effect_that_returns_no_proof_is_refused() -> None:
    latch = CloseOnce()

    with pytest.raises(TypeError, match="must return Released"):
        latch.run(lambda: None)  # ty: ignore[invalid-argument-type]

    assert latch.released is None
