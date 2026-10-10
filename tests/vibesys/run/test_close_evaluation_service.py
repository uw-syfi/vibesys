"""A second interrupt during the run host's cleanup ends the run cancelled (#1634).

The first Ctrl-C stops the run and its cleanup begins; the second cancels the
run's task while cleanup is mid-way. The cleanup must still release everything
and the run must end cancelled, not as ``RunCleanupError: evaluation agent
cleanup failed: CancelledError``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

# test-isolation: the factory's cleanup order is the behavior under test and the
# run host reaches it only through its private factory.
from vibesys.run.host import _ProductHostFactory
from vs_runtime.api import RunCleanupError

if TYPE_CHECKING:
    from collections.abc import Callable


class _Resource:
    """A run resource whose close can be cancelled by the second interrupt."""

    def __init__(self, name: str, closed: list[str], *, interrupt: Callable[[], object]) -> None:
        self._name = name
        self._closed = closed
        self._interrupt = interrupt
        self.interrupted_during_close = False

    async def close(self) -> None:
        if self.interrupted_during_close:
            self._interrupt()
        await asyncio.sleep(0)
        self._closed.append(self._name)


def _factory(backend: _Resource, provision: _Resource, services: _Resource) -> _ProductHostFactory:
    factory = _ProductHostFactory(*([None] * 11))  # ty: ignore[invalid-argument-type]
    factory.evaluation_backend = cast("object", backend)  # ty: ignore[invalid-assignment]
    factory.profiler_provision = cast("object", provision)  # ty: ignore[invalid-assignment]
    factory.core_services = cast("object", services)  # ty: ignore[invalid-assignment]
    return factory


@pytest.mark.parametrize("interrupted", ["provision", "backend", "services"])
def test_a_cancellation_during_any_close_step_ends_cancelled_after_every_step(
    interrupted: str,
) -> None:
    closed: list[str] = []

    async def run() -> BaseException | None:
        outcomes: list[BaseException | None] = []

        async def caller() -> None:
            task = asyncio.current_task()
            assert task is not None
            resources = {
                name: _Resource(name, closed, interrupt=task.cancel)
                for name in ("provision", "backend", "services")
            }
            resources[interrupted].interrupted_during_close = True
            factory = _factory(resources["backend"], resources["provision"], resources["services"])
            try:
                await factory.close_evaluation_service()
            except BaseException as error:
                outcomes.append(error)
                raise
            outcomes.append(None)

        await asyncio.gather(asyncio.create_task(caller()), return_exceptions=True)
        return outcomes[0]

    outcome = asyncio.run(run())

    assert isinstance(outcome, asyncio.CancelledError)
    assert not isinstance(outcome, RunCleanupError)
    assert closed == ["provision", "backend", "services"]
