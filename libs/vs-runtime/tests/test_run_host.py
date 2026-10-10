"""Public lifecycle contract for the runtime-owned run host."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from vs_evaluation.api import ExecutorCancellationUnknownError
from vs_runtime.api import RunCleanupError, RunFacts
from vs_runtime.api.infrastructure import BlockingOperations, RunHostComponents, open_run_host
from vs_runtime.api.testing import (
    FakeCommands,
    FakeControl,
    FakeEvaluation,
    FakeObservations,
    FakeSkills,
    FakeState,
    FakeWorkspace,
    FakeWorkspaceAgentSessions,
    FakeWorkspaces,
)
from vs_sim.api.testing import GatedBlockingRunner, arrival

if TYPE_CHECKING:
    from contextlib import ExitStack
    from pathlib import Path


def _joined_cleanup_failure(errors: list[BaseException]) -> BaseExceptionGroup:
    return BaseExceptionGroup("joined runtime cleanup failed", errors)


class _LifecycleAgents(FakeWorkspaceAgentSessions):
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
    ) -> None:
        self._events = events
        self._failure = failure
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        self._events.append("resources-close")
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
    runner: GatedBlockingRunner | None = None,
) -> tuple[RunHostComponents, BlockingOperations, _Resources]:
    blocking = BlockingOperations(runner)
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
        ),
        blocking,
        owner,
    )


def _prepare(
    ownership: ExitStack,
    components: RunHostComponents,
    resources: _Resources,
) -> RunHostComponents:
    ownership.callback(resources.close)
    return components


async def test_host_exposes_capabilities_and_closes_once_in_dependency_order(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    components, _blocking, resources = _components(tmp_path, events)
    async with open_run_host(lambda ownership: _prepare(ownership, components, resources)) as host:
        assert host.run.run_id == "run-1"
        assert host.run.facts.objective == "Test the host."
        assert not hasattr(host.run, "log")
        assert not hasattr(host.run, "close")
        host.run.observations.note("hello")

    assert isinstance(components.observations, FakeObservations)
    assert components.observations.calls[0].message == "hello"
    assert events == [
        "agents-begin-close",
        "agents-close",
        "workspaces-close",
        "resources-close",
    ]
    assert resources.close_count == 1


async def test_concurrent_close_is_idempotent(tmp_path: Path) -> None:
    events: list[str] = []
    components, _blocking, resources = _components(tmp_path, events)
    async with open_run_host(lambda ownership: _prepare(ownership, components, resources)) as host:
        await asyncio.gather(host.close(), host.close(), host.close())

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
        async with open_run_host(lambda ownership: _prepare(ownership, components, resources)):
            pass

    with pytest.raises(RunCleanupError) as caught:
        asyncio.run(exercise())

    workspace_errors = caught.value.failures[0]
    assert isinstance(workspace_errors, BaseExceptionGroup)
    assert [str(error) for error in workspace_errors.exceptions] == [
        "agent close",
        "workspace close",
    ]
    assert str(caught.value.failures[1]) == "resource close"
    assert events[-3:] == ["agents-close", "workspaces-close", "resources-close"]


@pytest.mark.parametrize(
    "failure",
    [
        ExecutorCancellationUnknownError("possibly-dispatched"),
        asyncio.CancelledError(),
        BaseExceptionGroup(
            "provider cleanup",
            [ExecutorCancellationUnknownError("possibly-dispatched"), asyncio.CancelledError()],
        ),
    ],
)
def test_cleanup_retains_unresolved_outcomes_as_a_typed_failure(
    tmp_path: Path, failure: BaseException
) -> None:
    events: list[str] = []
    resources = _Resources(events, failure=failure)
    components, _blocking, _resources = _components(tmp_path, events, resources=resources)

    async def exercise() -> None:
        async with open_run_host(lambda ownership: _prepare(ownership, components, resources)):
            pass

    with pytest.raises(RunCleanupError) as caught:
        asyncio.run(exercise())

    assert caught.value.failures == (failure,)
    assert resources.close_count == 1
    assert events[-3:] == ["agents-close", "workspaces-close", "resources-close"]


def test_body_failure_remains_primary_when_cleanup_fails(tmp_path: Path) -> None:
    events: list[str] = []
    components, _blocking, _resources = _components(
        tmp_path,
        events,
        resources=_Resources(events, failure=OSError("resource close")),
    )

    async def exercise() -> None:
        async with open_run_host(lambda ownership: _prepare(ownership, components, _resources)):
            raise _BodyFailureError

    with pytest.raises(ValueError, match="body failed") as caught:
        asyncio.run(exercise())

    assert any("runtime cleanup also failed" in note for note in caught.value.__notes__)


async def test_cancellation_during_prepare_closes_the_prepared_host(tmp_path: Path) -> None:
    events: list[str] = []
    runner = GatedBlockingRunner(held=True)
    components, _blocking, resources = _components(tmp_path, events, runner=runner)

    def prepare(ownership: ExitStack) -> RunHostComponents:
        ownership.callback(resources.close)
        return components

    async def use_host() -> None:
        async with open_run_host(prepare, runner=runner):
            pytest.fail("cancelled preparation yielded a host")

    task = asyncio.create_task(use_host())
    await runner.wait_in_flight(1, task)
    task.cancel()
    runner.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert resources.close_count == 1


async def test_prepare_failure_during_cancellation_keeps_cancellation_primary() -> None:
    resources = _Resources([])
    runner = GatedBlockingRunner(held=True)

    def prepare(ownership: ExitStack) -> RunHostComponents:
        ownership.callback(resources.close)
        raise _PreparationFailureError

    async def use_host() -> None:
        async with open_run_host(prepare, runner=runner):
            pytest.fail("failed preparation yielded a host")

    task = asyncio.create_task(use_host())
    await runner.wait_in_flight(1, task)
    task.cancel()
    runner.release()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert any("runtime preparation also failed" in note for note in caught.value.__notes__)
    assert resources.close_count == 1


def test_prepare_failure_closes_partial_resources_without_replacing_error() -> None:
    events: list[str] = []
    resources = _Resources(events, failure=OSError("resource close"))

    def prepare(ownership: ExitStack) -> RunHostComponents:
        ownership.callback(resources.close)
        raise _PreparationFailureError

    async def exercise() -> None:
        async with open_run_host(prepare):
            pytest.fail("failed preparation yielded a host")

    with pytest.raises(_PreparationFailureError, match="prepare failed") as caught:
        asyncio.run(exercise())

    assert resources.close_count == 1
    assert events == ["resources-close"]
    assert any(
        "runtime preparation cleanup also failed: resource close" in note
        for note in caught.value.__notes__
    )


async def test_cancellation_during_close_waits_for_cleanup_then_propagates(tmp_path: Path) -> None:
    events: list[str] = []
    runner = GatedBlockingRunner()
    resources = _Resources(events)
    components, _blocking, _resources = _components(
        tmp_path, events, resources=resources, runner=runner
    )

    async def use_host() -> None:
        async with open_run_host(
            lambda ownership: _prepare(ownership, components, resources), runner=runner
        ):
            runner.hold()

    task = asyncio.create_task(use_host())
    await runner.wait_in_flight(1, task)
    task.cancel()
    runner.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert resources.close_count == 1


async def test_close_drains_blocking_work_before_capabilities_and_resources(tmp_path: Path) -> None:
    events: list[str] = []
    runner = GatedBlockingRunner()

    def operation() -> str:
        events.append("operation-finished")
        return "done"

    close_started = asyncio.Event()
    components, blocking, resources = _components(
        tmp_path,
        events,
        begin_close_started=close_started,
        runner=runner,
    )
    async with open_run_host(
        lambda ownership: _prepare(ownership, components, resources), runner=runner
    ) as host:
        runner.hold()
        operation_task = asyncio.create_task(blocking.run(operation))
        await runner.wait_in_flight(1, operation_task)
        close_task = asyncio.create_task(host.close())
        await arrival(close_started.wait(), close_task)
        assert resources.close_count == 0
        runner.release()
        assert await operation_task == "done"
        await close_task
    assert events.index("operation-finished") < events.index("agents-close")
