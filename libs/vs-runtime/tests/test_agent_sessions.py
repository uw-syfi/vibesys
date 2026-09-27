"""Production explicit-session contracts through the composition API."""

from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import BaseModel

from vs_agent.api import AgentCapabilities, AgentSessionKey, SessionScope
from vs_agent.api import AgentTurnTimeoutError as DriverAgentTurnTimeoutError
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentTool,
    AgentTurnTimeoutError,
    RuntimeContractError,
    SessionClosedError,
    Workspace,
    WorkspaceAccess,
)
from vs_runtime.api.infrastructure import (
    AgentExecution,
    create_agent_session_runtime,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import ToolServerDescriptor


class _Reply(BaseModel):
    value: int


class _Workspace:
    def __init__(self, workspace_id: str = "root") -> None:
        self.id = workspace_id
        self.path = Path(f"/{workspace_id}")
        self.revision: str | None = None
        self.trusted_input_baseline: str | None = "input"
        self.changes: list[str] = []
        self.committed_changes: list[tuple[str, ...]] = []
        self.directories: set[str] = set()
        self.snapshots: list[str] = []

    async def snapshot(self, label: str) -> str:
        self.snapshots.append(label)
        self.committed_changes.append(tuple(self.changes))
        self.revision = f"revision-{len(self.snapshots)}"
        self.changes.clear()
        return self.revision

    async def restore(self, revision: str, *, clean: bool = True) -> None:
        del clean
        self.revision = revision
        self.changes.clear()

    async def try_restore(self, revision: str, *, clean: bool = True) -> bool:
        await self.restore(revision, clean=clean)
        return True

    async def retain(self, revision: str, *, label: str) -> None:
        del revision, label

    async def pending_changes(self) -> list[str]:
        return list(self.changes)

    async def restore_for_agent(
        self,
        revision: str,
        *,
        preserve_paths: tuple[str, ...],
    ) -> None:
        self.revision = revision
        self.changes = [
            path
            for path in self.changes
            if any(path == allowed or path.startswith(f"{allowed}/") for allowed in preserve_paths)
        ]

    def is_directory(self, path: str) -> bool:
        return path in self.directories


class _Execution:
    def __init__(
        self,
        *results: str | dict[str, int] | BaseException,
        capabilities: AgentCapabilities | None = None,
        on_turn: Callable[[], None] | None = None,
        close_log: list[str] | None = None,
        name: str = "execution",
    ) -> None:
        self.capabilities = capabilities or AgentCapabilities(
            session_reuse=True,
            provider_session_resume=True,
            tool_servers=True,
        )
        self.backend_name = "fake"
        self.driver_name = "fake-driver"
        self.provider = "fake-provider"
        self.model = "fake-model"
        self.reasoning_effort = "high"
        self._results = deque(results)
        self._on_turn = on_turn
        self._close_log = close_log
        self._name = name
        self.calls: list[
            tuple[str, str, type[BaseModel] | None, AgentSessionKey, tuple[str, ...]]
        ] = []
        self.closed = False

    async def execute[ResponseT: BaseModel](  # noqa: PLR0913  # lint-waiver: LW-911101 [PLR0913]; the Fake implements the temporary composition port exactly so its contract cannot drift.
        self,
        message: str,
        *,
        system_prompt: str,
        response: type[ResponseT] | None,
        label: str,
        session_key: AgentSessionKey,
        tool_servers: tuple[ToolServerDescriptor, ...] | None,
    ) -> str | ResponseT:
        del label
        self.calls.append(
            (
                message,
                system_prompt,
                response,
                session_key,
                tuple(item.name for item in tool_servers or ()),
            )
        )
        if self._on_turn is not None:
            self._on_turn()
        result = self._results.popleft()
        if isinstance(result, BaseException):
            raise result
        if response is None:
            assert isinstance(result, str)
            return result
        return response.model_validate(result)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._close_log is not None:
            self._close_log.append(self._name)


class _ExecutionFactory:
    def __init__(self, *executions: _Execution) -> None:
        self._executions = deque(executions)

    async def __call__(
        self,
        _role: AgentRole,
        _workspace: Workspace,
        _registration_id: str,
    ) -> AgentExecution:
        return self._executions.popleft()


class _BlockingExecution(_Execution):
    def __init__(self) -> None:
        super().__init__("first", "second")
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.active = 0
        self.max_active = 0

    async def execute[ResponseT: BaseModel](  # noqa: PLR0913  # lint-waiver: LW-911102 [PLR0913]; the concurrency Fake implements the temporary composition port exactly.
        self,
        message: str,
        *,
        system_prompt: str,
        response: type[ResponseT] | None,
        label: str,
        session_key: AgentSessionKey,
        tool_servers: tuple[ToolServerDescriptor, ...] | None,
    ) -> str | ResponseT:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if message == "first":
                self.started.set()
                await self.release.wait()
            return await super().execute(
                message,
                system_prompt=system_prompt,
                response=response,
                label=label,
                session_key=session_key,
                tool_servers=tool_servers,
            )
        finally:
            self.active -= 1


class _BlockingCloseExecution(_Execution):
    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        await super().close()


def _workspace(value: Workspace) -> _Workspace:
    if not isinstance(value, _Workspace):
        message = "workspace is not owned by this runtime"
        raise TypeError(message)
    return value


def test_session_fixes_role_tools_binding_and_conversation_identity() -> None:
    role = AgentRole(
        id="worker",
        system_prompt="Work carefully.",
        tools=(AgentTool(id="shell"), AgentTool(id="board")),
    )
    execution = _Execution("first", {"value": 2})
    tool_resolutions = 0

    class _Tool:
        name = "board"
        command = "board-server"
        args: tuple[str, ...] = ()
        env: tuple[tuple[str, str], ...] = ()

    def tools(_selected: Workspace) -> tuple[ToolServerDescriptor, ...]:
        nonlocal tool_resolutions
        tool_resolutions += 1
        return (cast("ToolServerDescriptor", _Tool()),)

    async def scenario() -> None:
        runtime = create_agent_session_runtime(
            (role,),
            open_execution=_ExecutionFactory(execution),
            resolve_workspace=_workspace,
            tool_bindings={"board": tools},
            log=lambda _message: None,
        )
        selected = _Workspace()
        session = await runtime.create_session(
            role,
            workspace=selected,
            member_id="candidate-1",
        )
        assert await session.turn("one") == "first"
        assert await session.turn("two", response=_Reply) == _Reply(value=2)
        assert session.binding.model == "fake-model"
        await runtime.close()

    asyncio.run(scenario())

    assert tool_resolutions == 1
    assert [call[0] for call in execution.calls] == ["one", "two"]
    assert all(call[1] == "Work carefully." for call in execution.calls)
    assert all(
        call[3] == AgentSessionKey(SessionScope.MEMBER, "worker:candidate-1")
        for call in execution.calls
    )
    assert all(call[4] == ("board",) for call in execution.calls)
    assert execution.closed


@pytest.mark.parametrize(
    ("access", "grants", "directories", "changes", "remaining"),
    [
        (WorkspaceAccess.READ_ONLY, (), (), ["source.py"], []),
        (
            WorkspaceAccess.LIMITED,
            ("evidence",),
            ("evidence",),
            ["evidence/report.json", "source.py"],
            ["evidence/report.json"],
        ),
        (
            WorkspaceAccess.LIMITED,
            ("evidence/report.json",),
            (),
            ["evidence/report.json", "evidence/sibling.json"],
            ["evidence/report.json"],
        ),
    ],
)
def test_session_enforces_declared_workspace_access(
    access: WorkspaceAccess,
    grants: tuple[str, ...],
    directories: tuple[str, ...],
    changes: list[str],
    remaining: list[str],
) -> None:
    role = AgentRole(id="worker", system_prompt="Work.", workspace_access=access)
    workspace = _Workspace()
    workspace.directories.update(directories)
    execution = _Execution("done", on_turn=lambda: workspace.changes.extend(changes))

    async def scenario() -> None:
        runtime = create_agent_session_runtime(
            (role,),
            open_execution=_ExecutionFactory(execution),
            resolve_workspace=_workspace,
            log=lambda _message: None,
        )
        session = await runtime.create_session(
            role,
            workspace=workspace,
            writable_paths=grants,
        )
        assert await session.turn("work") == "done"
        assert workspace.changes == []
        assert workspace.committed_changes[-1] == tuple(remaining)
        await runtime.close()

    asyncio.run(scenario())


def test_timeout_is_normalized_and_capability_failure_closes_execution() -> None:
    timeout = DriverAgentTurnTimeoutError(4.5)
    role = AgentRole(
        id="worker",
        system_prompt="Work.",
        required_capabilities=frozenset({AgentCapability.PROVIDER_SESSION_RESUME}),
    )
    timed = _Execution(timeout)
    unsupported = _Execution(capabilities=AgentCapabilities(session_reuse=True))

    async def scenario() -> None:
        runtime = create_agent_session_runtime(
            (role,),
            open_execution=_ExecutionFactory(timed, unsupported),
            resolve_workspace=_workspace,
            log=lambda _message: None,
        )
        session = await runtime.create_session(role, workspace=_Workspace())
        with pytest.raises(AgentTurnTimeoutError) as raised:
            await session.turn("work")
        assert raised.value.__cause__ is timeout
        with pytest.raises(RuntimeContractError, match="provider_session_resume"):
            await runtime.create_session(role, workspace=_Workspace(), member_id="durable")
        await runtime.close()

    asyncio.run(scenario())
    assert unsupported.closed


def test_close_is_reverse_order_idempotent_and_invalidates_workspace() -> None:
    closed: list[str] = []
    first = _Execution(close_log=closed, name="first")
    second = _Execution(close_log=closed, name="second")
    role = AgentRole(id="worker", system_prompt="Work.")

    async def scenario() -> None:
        runtime = create_agent_session_runtime(
            (role,),
            open_execution=_ExecutionFactory(first, second),
            resolve_workspace=_workspace,
            log=lambda _message: None,
        )
        selected = _Workspace()
        selected_session = await runtime.create_session(role, workspace=selected)
        await runtime.create_session(role, workspace=_Workspace("other"))
        runtime.invalidate_workspace(selected)
        with pytest.raises(SessionClosedError):
            await selected_session.turn("too late")
        await runtime.close()
        await runtime.close()
        with pytest.raises(SessionClosedError):
            await runtime.create_session(role, workspace=_Workspace("late"))

    asyncio.run(scenario())
    assert closed == ["second", "first"]


def test_turns_on_one_session_are_serialized() -> None:
    execution = _BlockingExecution()
    role = AgentRole(id="worker", system_prompt="Work.")

    async def scenario() -> None:
        runtime = create_agent_session_runtime(
            (role,),
            open_execution=_ExecutionFactory(execution),
            resolve_workspace=_workspace,
            log=lambda _message: None,
        )
        session = await runtime.create_session(role, workspace=_Workspace())
        first = asyncio.create_task(session.turn("first"))
        await execution.started.wait()
        second_attempted = asyncio.Event()

        async def second_turn() -> str:
            second_attempted.set()
            return await session.turn("second")

        second = asyncio.create_task(second_turn())
        await second_attempted.wait()
        assert execution.calls == []
        execution.release.set()
        assert await first == "first"
        assert await second == "second"
        assert execution.max_active == 1
        await runtime.close()

    asyncio.run(scenario())


def test_cancelled_close_waiter_does_not_cancel_owned_cleanup() -> None:
    execution = _BlockingCloseExecution()
    role = AgentRole(id="worker", system_prompt="Work.")

    async def scenario() -> None:
        runtime = create_agent_session_runtime(
            (role,),
            open_execution=_ExecutionFactory(execution),
            resolve_workspace=_workspace,
            log=lambda _message: None,
        )
        session = await runtime.create_session(role, workspace=_Workspace())
        waiter = asyncio.create_task(session.close())
        await execution.close_started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        execution.release_close.set()
        await session.close()
        assert execution.closed

    asyncio.run(scenario())
