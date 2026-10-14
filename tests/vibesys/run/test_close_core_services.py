"""A second interrupt during the run host's cleanup ends the run cancelled (#1634).

The first Ctrl-C stops the run and its cleanup begins; the second cancels the
run's task while cleanup is mid-way. The cleanup must still release everything
and the run must end cancelled, not as ``RunCleanupError: cleanup failed: CancelledError``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

# test-isolation: the factory's cleanup order is the behavior under test and the
# run host reaches it only through its private factory.
from vibesys.run.host import _ProductHostFactory
from vs_runtime.api import RunCleanupError
from vs_sim.api import yield_now

if TYPE_CHECKING:
    from collections.abc import Callable


class _Services:
    """Core services whose close is cancelled by the second interrupt."""

    def __init__(self, closed: list[str], *, interrupt: Callable[[], object]) -> None:
        self._closed = closed
        self._interrupt = interrupt

    async def close(self) -> None:
        self._interrupt()
        await yield_now()
        self._closed.append("services")


def _factory(services: _Services) -> _ProductHostFactory:
    factory = _ProductHostFactory(*([None] * 11))  # ty: ignore[invalid-argument-type]
    factory.core_services = cast("object", services)  # ty: ignore[invalid-assignment]
    return factory


def test_a_cancellation_during_the_services_close_ends_cancelled_after_the_close() -> None:
    closed: list[str] = []

    async def run() -> BaseException | None:
        outcomes: list[BaseException | None] = []

        async def caller() -> None:
            task = asyncio.current_task()
            assert task is not None
            factory = _factory(_Services(closed, interrupt=task.cancel))
            try:
                await factory.close_core_services()
            except BaseException as error:
                outcomes.append(error)
                raise
            outcomes.append(None)

        await asyncio.gather(asyncio.create_task(caller()), return_exceptions=True)
        return outcomes[0]

    outcome = asyncio.run(run())

    assert isinstance(outcome, asyncio.CancelledError)
    assert not isinstance(outcome, RunCleanupError)
    assert closed == ["services"]
