"""Public lifecycle contract for the runtime-owned run host."""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

import pytest

from vs_runtime.api import RunFacts
from vs_runtime.api.infrastructure import BlockingOperations, RunHostComponents, open_run_host
from vs_runtime.api.testing import (
    FakeAgentSessions,
    FakeCommands,
    FakeControl,
    FakeEvaluation,
    FakeObservations,
    FakeSkills,
    FakeState,
    FakeWorkspace,
    FakeWorkspaces,
)

if TYPE_CHECKING:
    from pathlib import Path


def _joined_cleanup_failure(errors: list[BaseException]) -> BaseExceptionGroup:
    return BaseExceptionGroup("joined runtime cleanup failed", errors)


class _LifecycleAgents(FakeAgentSessions):
    def __init__(
        self,
        events: list[str],
        failure: BaseException | None = None,
        begin_close_started: asyncio.Event | None = None,
    ) -> None:
        super().__init__(())
        self._events = events
        self._failure = failure
        self._begin_close_started = begin_close_started

    def begin_close(self) -> None:
        self._events.append("agents-begin-close")
        if self._begin_close_started is not None:
            self._begin_close_started.set()
        super().begin_close()

    async def close(self) -> None:
        self._events.append("agents-close")
        if self._failure is not None:
            raise self._failure
        await super().close()


class _LifecycleWorkspaces(FakeWorkspaces):
    def __init__(
        self,
        path: Path,
        events: list[str],
        agents: _LifecycleAgents,
        failure: BaseException | None = None,
    ) -> None:
        super().__init__(FakeWorkspace(path=path), sessions=agents)
        self._events = events
        self._agents = agents
        self._failure = failure

    def begin_close(self) -> None:
        self._agents.begin_close()

    async def close(self) -> None:
        errors: list[BaseException] = []
        try:
            await self._agents.close()
        except BaseException as error:  # noqa: BLE001  # lint-waiver: LW-948024 [BLE001]; the lifecycle fake must model joined-owner cleanup through every failure.
            errors.append(error)
        self._events.append("workspaces-close")
        if self._failure is not None:
            errors.append(self._failure)
        if errors:
            raise _joined_cleanup_failure(errors)


class _Resources:
    def __init__(
        self,
        events: list[str],
        *,
        failure: BaseException | None = None,
        close_started: threading.Event | None = None,
        close_release: threading.Event | None = None,
    ) -> None:
        self._events = events
        self._failure = failure
        self._close_started = close_started
        self._close_release = close_release
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        self._events.append("resources-close")
        if self._close_started is not None:
            self._close_started.set()
        if self._close_release is not None:
            self._close_release.wait()
        if self._failure is not None:
            raise self._failure


class _BodyFailureError(ValueError):
    def __init__(self) -> None:
        super().__init__("body failed")


class _PreparationFailureError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("prepare failed")


def _components(  # noqa: PLR0913  # lint-waiver: LW-948021 [PLR0913]; lifecycle cases independently select failure points and synchronization while sharing one valid component assembly.
    tmp_path: Path,
    events: list[str],
    *,
    agent_failure: BaseException | None = None,
    workspace_failure: BaseException | None = None,
    resources: _Resources | None = None,
    begin_close_started: asyncio.Event | None = None,
) -> tuple[RunHostComponents, BlockingOperations, _Resources]:
    blocking = BlockingOperations()
    agents = _LifecycleAgents(events, agent_failure, begin_close_started)
    workspaces = _LifecycleWorkspaces(tmp_path, events, agents, workspace_failure)
    owner = resources or _Resources(events)
    return (
        RunHostComponents(
            run_id="run-1",
            facts=RunFacts(domain_id="generic", objective="Test the host."),
            agents=agents,
            workspaces=workspaces,
            evaluation=FakeEvaluation(run_id="run-1"),
            state=FakeState(None, workspaces.root),
            control=FakeControl(),
            commands=FakeCommands(),
            skills=FakeSkills(),
            observations=FakeObservations(),
            blocking=blocking,
            resources=owner,
        ),
        blocking,
        owner,
    )


def test_host_exposes_capabilities_and_closes_once_in_dependency_order(tmp_path: Path) -> None:
    events: list[str] = []
    components, _blocking, resources = _components(tmp_path, events)

    async def exercise() -> None:
        async with open_run_host(lambda: components) as host:
            assert host.run.run_id == "run-1"
            assert host.run.facts.objective == "Test the host."
            assert not hasattr(host.run, "log")
            assert not hasattr(host.run, "close")
            host.run.observations.note("hello")

    asyncio.run(exercise())

    assert isinstance(components.observations, FakeObservations)
    assert components.observations.calls[0].message == "hello"
    assert events == [
        "agents-begin-close",
        "agents-close",
        "workspaces-close",
        "resources-close",
    ]
    assert resources.close_count == 1


def test_concurrent_close_is_idempotent(tmp_path: Path) -> None:
    events: list[str] = []
    components, _blocking, resources = _components(tmp_path, events)

    async def exercise() -> None:
        async with open_run_host(lambda: components) as host:
            await asyncio.gather(host.close(), host.close(), host.close())

    asyncio.run(exercise())

    assert events.count("agents-close") == 1
    assert events.count("workspaces-close") == 1
    assert resources.close_count == 1


def test_cleanup_attempts_every_owner_and_aggregates_failures(tmp_path: Path) -> None:
    events: list[str] = []
    resources = _Resources(events, failure=OSError("resource close"))
    components, _blocking, _resources = _components(
        tmp_path,
        events,
        agent_failure=ValueError("agent close"),
        workspace_failure=RuntimeError("workspace close"),
        resources=resources,
    )

    async def exercise() -> None:
        async with open_run_host(lambda: components):
            pass

    with pytest.raises(BaseExceptionGroup) as caught:
        asyncio.run(exercise())

    workspace_errors = caught.value.exceptions[0]
    assert isinstance(workspace_errors, BaseExceptionGroup)
    assert [str(error) for error in workspace_errors.exceptions] == [
        "agent close",
        "workspace close",
    ]
    assert str(caught.value.exceptions[1]) == "resource close"
    assert events[-3:] == ["agents-close", "workspaces-close", "resources-close"]


def test_body_failure_remains_primary_when_cleanup_fails(tmp_path: Path) -> None:
    events: list[str] = []
    components, _blocking, _resources = _components(
        tmp_path,
        events,
        resources=_Resources(events, failure=OSError("resource close")),
    )

    async def exercise() -> None:
        async with open_run_host(lambda: components):
            raise _BodyFailureError

    with pytest.raises(ValueError, match="body failed") as caught:
        asyncio.run(exercise())

    assert any("runtime cleanup also failed" in note for note in caught.value.__notes__)


def test_cancellation_during_prepare_closes_the_prepared_host(tmp_path: Path) -> None:
    events: list[str] = []
    components, _blocking, resources = _components(tmp_path, events)
    started = threading.Event()
    release = threading.Event()

    def prepare() -> RunHostComponents:
        started.set()
        release.wait()
        return components

    async def exercise() -> None:
        async def use_host() -> None:
            async with open_run_host(prepare):
                pytest.fail("cancelled preparation yielded a host")

        task = asyncio.create_task(use_host())
        await asyncio.to_thread(started.wait)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert resources.close_count == 1


def test_prepare_failure_during_cancellation_keeps_cancellation_primary(tmp_path: Path) -> None:
    del tmp_path
    started = threading.Event()
    release = threading.Event()

    def prepare() -> RunHostComponents:
        started.set()
        release.wait()
        raise _PreparationFailureError

    async def exercise() -> None:
        async def use_host() -> None:
            async with open_run_host(prepare):
                pytest.fail("failed preparation yielded a host")

        task = asyncio.create_task(use_host())
        await asyncio.to_thread(started.wait)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert isinstance(caught.value.__cause__, RuntimeError)
        assert any("runtime preparation also failed" in note for note in caught.value.__notes__)

    asyncio.run(exercise())


def test_cancellation_during_close_waits_for_cleanup_then_propagates(tmp_path: Path) -> None:
    events: list[str] = []
    close_started = threading.Event()
    close_release = threading.Event()
    resources = _Resources(
        events,
        close_started=close_started,
        close_release=close_release,
    )
    components, _blocking, _resources = _components(tmp_path, events, resources=resources)

    async def exercise() -> None:
        async def use_host() -> None:
            async with open_run_host(lambda: components):
                pass

        task = asyncio.create_task(use_host())
        await asyncio.to_thread(close_started.wait)
        task.cancel()
        close_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert resources.close_count == 1


def test_close_drains_blocking_work_before_capabilities_and_resources(tmp_path: Path) -> None:
    events: list[str] = []
    operation_started = threading.Event()
    operation_release = threading.Event()

    def operation() -> str:
        operation_started.set()
        operation_release.wait()
        events.append("operation-finished")
        return "done"

    async def exercise() -> None:
        close_started = asyncio.Event()
        components, blocking, resources = _components(
            tmp_path,
            events,
            begin_close_started=close_started,
        )
        async with open_run_host(lambda: components) as host:
            operation_task = asyncio.create_task(blocking.run(operation))
            await asyncio.to_thread(operation_started.wait)
            close_task = asyncio.create_task(host.close())
            await close_started.wait()
            assert resources.close_count == 0
            operation_release.set()
            assert await operation_task == "done"
            await close_task

    asyncio.run(exercise())
    assert events.index("operation-finished") < events.index("agents-close")
