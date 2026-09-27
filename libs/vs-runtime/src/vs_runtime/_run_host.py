"""Run-owned orchestration host lifecycle."""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from vs_runtime._run_control import RunControlChannel
    from vs_runtime._workspaces import OwnedWorkspaces
    from vs_runtime.contracts import (
        AgentSessions,
        Commands,
        Control,
        Evaluation,
        RunFacts,
        Skills,
        State,
    )


class RunHostResourceOwner(Protocol):
    """Composition-owned synchronous resources released after runtime effects."""

    def close(self) -> None:
        """Release the product resources exactly once."""
        ...


def _runtime_closed_error() -> RuntimeError:
    return RuntimeError("runtime is closed")


def _cleanup_failure(errors: list[BaseException]) -> BaseExceptionGroup:
    return BaseExceptionGroup("run cleanup failed", errors)


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
    """Prepared capabilities and resource owners for one runtime host."""

    run_id: str
    facts: RunFacts
    agents: AgentSessions
    workspaces: OwnedWorkspaces
    evaluation: Evaluation
    state: State
    control: Control
    commands: Commands
    skills: Skills
    log: Callable[[str], None]
    blocking: BlockingOperations
    resources: RunHostResourceOwner


class RuntimeRunHost:
    """Concrete run host owning capability and resource teardown."""

    def __init__(self, components: RunHostComponents) -> None:
        self.run_id = components.run_id
        self.facts = components.facts
        self.agents = components.agents
        self.workspaces = components.workspaces
        self.evaluation = components.evaluation
        self.state = components.state
        self.control = components.control
        self.commands = components.commands
        self.skills = components.skills
        self._log = components.log
        self._blocking = components.blocking
        self._resources = components.resources
        self._close_task: asyncio.Task[None] | None = None

    def log(self, message: str) -> None:
        """Record one presentation-neutral run log message."""
        self._log(message)

    async def close(self) -> None:
        """Release all run-owned resources exactly once."""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_once())
        await asyncio.shield(self._close_task)

    async def _close_once(self) -> None:
        self._blocking.begin_close()
        self.workspaces.begin_close()
        errors = await self._blocking.drain()
        try:
            await self.workspaces.close()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-948002 [BLE001]; cleanup must continue through independently owned resources.
            errors.append(error)
        try:
            await asyncio.to_thread(self._resources.close)
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


@asynccontextmanager
async def open_run_host(
    prepare: Callable[[], RunHostComponents],
) -> AsyncIterator[RuntimeRunHost]:
    """Prepare and close one host, including on cancellation or setup failure."""
    host: RuntimeRunHost | None = None
    try:
        preparation = asyncio.create_task(asyncio.to_thread(prepare))
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
            host = RuntimeRunHost(components)
            raise
        host = RuntimeRunHost(components)
        yield host
    finally:
        if host is not None:
            await _close_runtime(host, sys.exception())


__all__ = [
    "BlockingOperations",
    "RunHostComponents",
    "RunHostResourceOwner",
    "RuntimeRunHost",
    "create_runtime_control",
    "open_run_host",
]
