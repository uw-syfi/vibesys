"""The run host's close: a teardown step that failed runs again on the next close."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_runtime.api import CaptureStopUnconfirmedError, RunCleanupError
from vs_runtime.api.infrastructure import BlockingOperations, RunHostComponents, open_run_host
from vs_runtime.api.testing import FakeWorkspace, FakeWorkspaces
from vs_sandbox.api import CommandExecutionSettledError
from vs_sim.api.testing import (
    GatedBlockingRunner,
    InlineBlockingRunner,
    SimThreads,
    VirtualClock,
    run_virtual,
)

if TYPE_CHECKING:
    from vs_runtime.api.infrastructure import RuntimeRunHost
    from vs_sim.api import Event, ReleaseStack, Threads


class _CancelSignalLostError(OSError):
    """The cooperative signal took effect but its acknowledgement was lost."""


class _SetThenFailEvent:
    """An event whose stop signal takes effect before ``set`` reports failure."""

    def __init__(self, delegate: Event) -> None:
        self._delegate = delegate

    def is_set(self) -> bool:
        return self._delegate.is_set()

    def set(self) -> None:
        self._delegate.set()
        raise _CancelSignalLostError

    def clear(self) -> None:
        self._delegate.clear()

    def wait(self, timeout: float | None = None) -> bool:
        return self._delegate.wait(timeout)


class _CancelSignalFailureThreads:
    """Thread primitives whose cooperative event loses the set acknowledgement."""

    def __init__(self, delegate: Threads) -> None:
        self._delegate = delegate

    def event(self) -> Event:
        return _SetThenFailEvent(self._delegate.event())

    def __getattr__(self, name: str) -> object:
        return getattr(self._delegate, name)


class _FlakyRelease:
    """A run-owned resource whose release raises ``failures`` times before it succeeds."""

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def close(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            message = "docker rm -f was killed"
            raise RuntimeError(message)


@given(failures=st.integers(1, 3), extra_closes=st.integers(0, 2))
def test_a_run_resource_whose_release_failed_is_released_again_on_the_next_close(
    failures: int, extra_closes: int
) -> None:
    """Regression: the host closed once, so a retried close never re-ran a failed release."""
    resource = _FlakyRelease(failures)
    runner = InlineBlockingRunner()

    def prepare(ownership: ReleaseStack) -> RunHostComponents:
        ownership.callback(resource.close)
        return RunHostComponents(
            workspaces=FakeWorkspaces(FakeWorkspace(path=Path("/project"))),
            blocking=BlockingOperations(runner, SimThreads()),
        )

    async def scenario() -> None:
        opened: list[RuntimeRunHost] = []
        with pytest.raises(RunCleanupError):
            async with open_run_host(prepare, runner=runner) as host:
                opened.append(host)
        for _ in range(failures - 1):
            with pytest.raises(RunCleanupError):
                await opened[0].close()
        for _ in range(1 + extra_closes):
            await opened[0].close()

    run_virtual(VirtualClock(), scenario())

    assert resource.calls == failures + 1


def test_cancellable_operation_preserves_typed_settlement_after_local_cleanup_failure() -> None:
    """A local timer fault cannot turn known remote settlement into retained ownership."""
    operations = BlockingOperations(InlineBlockingRunner(), SimThreads())
    cleanup_error = OSError("deadline join failed")

    def fail_after_settlement(_cancel: object) -> None:
        raise CommandExecutionSettledError(cleanup_error)

    async def scenario() -> None:
        with pytest.raises(CommandExecutionSettledError) as raised:
            await operations.run_cancellable(fail_after_settlement)
        assert raised.value.cause is cleanup_error

    run_virtual(VirtualClock(), scenario())


def test_caller_cancellation_cannot_erase_a_later_settlement_proof() -> None:
    """The drained worker's typed proof wins over the caller's cancellation."""
    runner = GatedBlockingRunner(held=True)
    operations = BlockingOperations(runner, SimThreads())
    cleanup_error = OSError("deadline join failed")

    def settle_after_cancel(cancel: Event) -> None:
        assert cancel.is_set()
        raise CommandExecutionSettledError(cleanup_error)

    async def scenario() -> None:
        operation = asyncio.create_task(operations.run_cancellable(settle_after_cancel))
        await runner.wait_in_flight(1, operation)
        operation.cancel()
        runner.release()
        with pytest.raises(CommandExecutionSettledError) as raised:
            await operation
        assert raised.value.cause is cleanup_error

    run_virtual(VirtualClock(), scenario())


@pytest.mark.parametrize("settled", [True, False])
def test_cancel_signal_failure_preserves_an_already_completed_typed_outcome(
    *, settled: bool
) -> None:
    """A failed signal acknowledgement cannot replace typed worker evidence."""
    runner = GatedBlockingRunner(held=True)
    simulator = SimThreads()
    threads = cast("Threads", _CancelSignalFailureThreads(simulator))
    operations = BlockingOperations(runner, threads)
    cleanup_error = OSError("deadline join failed")

    def finish_with_typed_outcome(_cancel: Event) -> None:
        if settled:
            raise CommandExecutionSettledError(cleanup_error)
        raise CaptureStopUnconfirmedError

    async def scenario() -> None:
        operation = asyncio.create_task(operations.run_cancellable(finish_with_typed_outcome))
        await runner.wait_in_flight(1, operation)
        runner.release()
        operation.cancel()
        expected = CommandExecutionSettledError if settled else CaptureStopUnconfirmedError
        with pytest.raises(expected) as raised:
            await operation
        if settled:
            assert isinstance(raised.value, CommandExecutionSettledError)
            assert raised.value.cause is cleanup_error

    run_virtual(VirtualClock(), scenario())
