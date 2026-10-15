"""Run-owned orchestration host lifecycle."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_runtime.contracts import RunCleanupError
from vs_sim.api import AsyncCloseOnce, ReleaseStack

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from vs_runtime._workspaces import OwnedWorkspaces
    from vs_sim.api import BlockingRunner, Released


def _runtime_closed_error() -> RuntimeError:
    return RuntimeError("runtime is closed")


def _cleanup_failure(errors: list[BaseException]) -> RunCleanupError:
    return RunCleanupError("run cleanup failed", tuple(errors))


class BlockingOperations:
    """Run synchronous effects without racing run teardown."""

    def __init__(self, runner: BlockingRunner) -> None:
        """Run operations on ``runner``."""
        self._runner = runner
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

    @property
    def runner(self) -> BlockingRunner:
        """Where this run's synchronous effects execute."""
        return self._runner

    async def run[**P, Result](
        self,
        operation: Callable[P, Result],
        /,
        *args: P.args,
        **kwargs: P.kwargs,
    ) -> Result:
        """Run one synchronous operation and preserve cancellation semantics."""
        if self._closed:
            raise _runtime_closed_error()
        task = asyncio.create_task(self._runner.run(operation, *args, **kwargs))
        self._tasks.add(task)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError as cancelled:
            await _wait_until_done(task)
            if error := task.exception():
                cancelled.add_note(f"blocking operation also failed: {error}")
            raise
        finally:
            self._tasks.discard(task)

    def begin_close(self) -> None:
        """Reject new work before owned tasks are drained."""
        self._closed = True

    async def drain(self) -> list[BaseException]:
        """Wait for every in-flight operation and return all failures."""
        errors: list[BaseException] = []
        for operation in tuple(self._tasks):
            await _wait_until_done(operation)
            if error := operation.exception():
                errors.append(error)
        return errors


@dataclass(frozen=True, slots=True)
class RunHostComponents:
    """Prepared resources for one runtime host: the workspace owner and the blocking executor."""

    workspaces: OwnedWorkspaces
    blocking: BlockingOperations


class RuntimeRunHost:
    """Concrete run host owning workspace and resource teardown."""

    def __init__(self, components: RunHostComponents, ownership: ReleaseStack) -> None:
        """Bind the fixed public value to its private lifecycle owner."""
        self.workspaces = components.workspaces
        self._blocking = components.blocking
        self._ownership = ownership
        self._close = AsyncCloseOnce()

    async def close(self) -> None:
        """Release all run-owned resources; a step that failed runs again on the next call."""
        await self._close.run(self._release)

    async def _release(self) -> Released:
        self._blocking.begin_close()
        self.workspaces.begin_close()
        errors = await self._blocking.drain()
        try:
            await self.workspaces.close()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-948002 [BLE001]; cleanup must continue through independently owned resources.
            errors.append(error)
        released: Released | None = None
        try:
            released = await self._blocking.runner.run(self._ownership.close)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-948003 [BLE001]; all cleanup outcomes are reported together after every owner runs.
            errors.append(error)
        if errors or released is None:
            raise _cleanup_failure(errors)
        return released


async def _wait_until_done[Result](task: asyncio.Task[Result]) -> None:
    """Wait for a worker even if its caller receives repeated cancellation."""
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            continue
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-948004 [BLE001]; the caller inspects the completed task outcome.
            break


async def _close_runtime(host: RuntimeRunHost, error: BaseException | None) -> None:
    """Drain cleanup while preserving the body failure or cancellation."""
    cancelled = False
    cleanup = asyncio.create_task(host.close())
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-948005 [BLE001]; the completed cleanup outcome is handled below.
            break
    try:
        cleanup.result()
    except BaseException as cleanup_error:
        if error is not None:
            error.add_note(f"runtime cleanup also failed: {cleanup_error}")
            return
        if cancelled and not isinstance(cleanup_error, asyncio.CancelledError):
            cancellation = asyncio.CancelledError()
            cancellation.add_note(f"runtime cleanup also failed: {cleanup_error}")
            raise cancellation from cleanup_error
        raise
    if cancelled and error is None:
        raise asyncio.CancelledError


async def _close_preparation_resources(
    ownership: ReleaseStack,
    error: BaseException | None,
    runner: BlockingRunner,
) -> None:
    """Close partially prepared resources without replacing the root failure."""
    cancelled = False
    cleanup = asyncio.create_task(runner.run(ownership.close))
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:  # noqa: BLE001  # lint-waiver: LW-948025 [BLE001]; the completed cleanup outcome is handled below.
            break
    try:
        cleanup.result()
    except BaseException as cleanup_error:
        if error is not None:
            error.add_note(f"runtime preparation cleanup also failed: {cleanup_error}")
            return
        if cancelled and not isinstance(cleanup_error, asyncio.CancelledError):
            cancellation = asyncio.CancelledError()
            cancellation.add_note(f"runtime preparation cleanup also failed: {cleanup_error}")
            raise cancellation from cleanup_error
        raise
    if cancelled and error is None:
        raise asyncio.CancelledError


@asynccontextmanager
async def open_run_host(
    prepare: Callable[[ReleaseStack], RunHostComponents],
    *,
    runner: BlockingRunner,
) -> AsyncIterator[RuntimeRunHost]:
    """Prepare and close one host, including on cancellation or setup failure.

    Preparation and failed-preparation cleanup run on ``runner``.
    """
    ownership = ReleaseStack()
    host: RuntimeRunHost | None = None
    try:
        preparation = asyncio.create_task(runner.run(prepare, ownership))
        try:
            components = await asyncio.shield(preparation)
        except asyncio.CancelledError as cancellation:
            await _wait_until_done(preparation)
            try:
                components = preparation.result()
            except BaseException as preparation_error:
                cancellation.add_note(
                    "runtime preparation also failed: "
                    f"{type(preparation_error).__name__}: {preparation_error}"
                )
                raise cancellation from preparation_error
            host = RuntimeRunHost(components, ownership)
            raise
        host = RuntimeRunHost(components, ownership)
        yield host
    finally:
        if host is not None:
            await _close_runtime(host, sys.exception())
        else:
            await _close_preparation_resources(ownership, sys.exception(), runner)


__all__ = [
    "BlockingOperations",
    "RunHostComponents",
    "RuntimeRunHost",
    "open_run_host",
]
