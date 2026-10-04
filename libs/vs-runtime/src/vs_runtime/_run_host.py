"""Run-owned orchestration host lifecycle."""

from __future__ import annotations

import asyncio
import sys
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from vs_runtime.contracts import Run, RunCleanupError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from vs_runtime._run_control import RunControlChannel
    from vs_runtime._workspaces import OwnedWorkspaces
    from vs_runtime.contracts import (
        Commands,
        Control,
        Evaluation,
        Observations,
        RunFacts,
        Skills,
        State,
        WorkspaceAgentSessions,
    )


def _runtime_closed_error() -> RuntimeError:
    return RuntimeError("runtime is closed")


def _cleanup_failure(errors: list[BaseException]) -> RunCleanupError:
    return RunCleanupError("run cleanup failed", tuple(errors))


class BlockingOperations:
    """Run synchronous effects without racing run teardown."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task] = set()
        self._closed = False

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
        task = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
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


class RuntimeControl:
    """Land cooperative stop and pause requests at policy checkpoints."""

    def __init__(self, channel: RunControlChannel, blocking: BlockingOperations) -> None:
        self._channel = channel
        self._blocking = blocking

    async def checkpoint(self) -> None:
        """Raise a pending stop or wait for a pending pause to resume."""
        self._channel.raise_if_stopped()
        await self._blocking.run(self._channel.wait_while_paused)


def create_runtime_control(
    channel: RunControlChannel,
    blocking: BlockingOperations,
) -> Control:
    """Bind cooperative control to the host's cancellation-safe worker owner."""
    return RuntimeControl(channel, blocking)


@dataclass(frozen=True, slots=True)
class RunHostComponents:
    """Prepared capabilities for one runtime host."""

    run_id: str
    facts: RunFacts
    agents: WorkspaceAgentSessions
    workspaces: OwnedWorkspaces
    evaluation: Evaluation
    state: State
    control: Control
    commands: Commands
    skills: Skills
    observations: Observations
    blocking: BlockingOperations


class RuntimeRunHost:
    """Concrete run host owning capability and resource teardown."""

    def __init__(self, components: RunHostComponents, ownership: ExitStack) -> None:
        """Bind the fixed public value to its private lifecycle owner."""
        self._workspaces = components.workspaces
        self.run = Run(
            run_id=components.run_id,
            facts=components.facts,
            agents=components.agents,
            workspaces=components.workspaces,
            evaluation=components.evaluation,
            state=components.state,
            control=components.control,
            commands=components.commands,
            skills=components.skills,
            observations=components.observations,
        )
        self._blocking = components.blocking
        self._ownership = ownership
        self._close_task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        """Release all run-owned resources exactly once."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        self._blocking.begin_close()
        self._workspaces.begin_close()
        errors = await self._blocking.drain()
        try:
            await self._workspaces.close()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-948002 [BLE001]; cleanup must continue through independently owned resources.
            errors.append(error)
        try:
            await asyncio.to_thread(self._ownership.close)
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-948003 [BLE001]; all cleanup outcomes are reported together after every owner runs.
            errors.append(error)
        if errors:
            raise _cleanup_failure(errors)


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
    ownership: ExitStack,
    error: BaseException | None,
) -> None:
    """Close partially prepared resources without replacing the root failure."""
    cancelled = False
    cleanup = asyncio.create_task(asyncio.to_thread(ownership.close))
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
    prepare: Callable[[ExitStack], RunHostComponents],
) -> AsyncIterator[RuntimeRunHost]:
    """Prepare and close one host, including on cancellation or setup failure."""
    ownership = ExitStack()
    host: RuntimeRunHost | None = None
    try:
        preparation = asyncio.create_task(asyncio.to_thread(prepare, ownership))
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
            await _close_preparation_resources(ownership, sys.exception())


__all__ = [
    "BlockingOperations",
    "RunHostComponents",
    "RuntimeRunHost",
    "create_runtime_control",
    "open_run_host",
]
