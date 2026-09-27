"""Public contracts for runtime-owned explicit agent sessions."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel

from vs_agent.api import (
    NULL_AGENT_EVENT_SINK,
    AgentBackend,
    AgentCapabilities,
    AgentSessionKey,
    AgentSpec,
    SessionScope,
)
from vs_agent.api import AgentTurnTimeoutError as DriverAgentTurnTimeoutError
from vs_agent.api.testing import FakeAgentClient
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentSession,
    AgentTool,
    AgentTurnTimeoutError,
    RuntimeContractError,
    SessionClosedError,
    Workspace,
    WorkspaceAccess,
)
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionFinished,
    AgentExecutionScope,
    AgentExecutionStarted,
    AgentExecutionStatus,
    AgentSessionRuntime,
    create_agent_session_runtime,
    create_run_control_channel,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionEnvironment,
    FakeAgentExecutionLifecycleSink,
    FakeAgentSessions,
    FakeRunControlEventSink,
    FakeWorkspace,
)
from vs_sandbox.api import ProjectPathPolicy

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import AgentClientProtocol, ToolServerDescriptor


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
        self.agent_restores: list[tuple[str, tuple[str, ...]]] = []

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
        self.agent_restores.append((revision, preserve_paths))
        self.revision = revision
        self.changes = [
            path
            for path in self.changes
            if any(path == allowed or path.startswith(f"{allowed}/") for allowed in preserve_paths)
        ]

    def is_directory(self, path: str) -> bool:
        return path in self.directories


class _SnapshotGate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def enter(self) -> None:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()


class _BlockedRuntimeWorkspace(_Workspace):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(workspace_id)
        self.gate = _SnapshotGate()

    async def snapshot(self, label: str) -> str:
        await self.gate.enter()
        return await super().snapshot(label)


class _BlockedFakeWorkspace(FakeWorkspace):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(workspace_id=workspace_id, path=Path(f"/{workspace_id}"))
        self.gate = _SnapshotGate()

    async def snapshot(self, label: str) -> str:
        await self.gate.enter()
        return await super().snapshot(label)


class _ClientFactory:
    def __init__(self, *clients: AgentClientProtocol) -> None:
        self._clients = deque(clients)
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> AgentClientProtocol:
        self.calls.append(kwargs)
        return self._clients.popleft()


class _EnvironmentOpener:
    def __init__(self, *environments: FakeAgentExecutionEnvironment) -> None:
        self._environments = deque(environments)
        self.configurations: list[AgentExecutionConfiguration] = []

    def __call__(self, configuration: AgentExecutionConfiguration) -> FakeAgentExecutionEnvironment:
        self.configurations.append(configuration)
        return self._environments.popleft()


def _workspace(value: Workspace) -> _Workspace:
    if not isinstance(value, _Workspace):
        message = "workspace is not owned by this runtime"
        raise TypeError(message)
    return value


def _scope(
    workspace: Workspace,
    opener: _EnvironmentOpener,
) -> AgentExecutionScope:
    selected = _workspace(workspace)
    return AgentExecutionScope(
        workspace_path=selected.path,
        log_directory=Path("/logs"),
        open_environment=opener,
        current_log_file=StringIO,
        environment_variables=lambda: {"CUDA_VISIBLE_DEVICES": "2"},
    )


def _runtime(
    role: AgentRole,
    clients: _ClientFactory,
    environments: _EnvironmentOpener,
    lifecycle: FakeAgentExecutionLifecycleSink,
    *,
    tool_bindings: dict[str, Callable[[Workspace], tuple[ToolServerDescriptor, ...]]] | None = None,
) -> AgentSessionRuntime:
    return create_agent_session_runtime(
        (role,),
        resolve_execution=lambda selected_role, workspace: (
            AgentExecutionConfiguration(
                agent_id=selected_role.id,
                spec=AgentSpec(backend=AgentBackend.STUB),
                reasoning_effort="high",
            ),
            _scope(workspace, environments),
        ),
        resolve_workspace=_workspace,
        session_store=lambda: None,
        control=create_run_control_channel(FakeRunControlEventSink()),
        lifecycle_events=lifecycle,
        agent_events=NULL_AGENT_EVENT_SINK,
        route_message=lambda message, steering: message + "".join(steering),
        client_factory=clients,
        tool_bindings=tool_bindings,
        log=lambda _message: None,
    )


def _client(*, responses: tuple[str, ...] = ()) -> FakeAgentClient:
    return FakeAgentClient(
        model="fake-model",
        capabilities=AgentCapabilities(
            session_reuse=True,
            provider_session_resume=True,
            tool_servers=True,
        ),
    ).enqueue_text("worker", *responses)


def _environment() -> FakeAgentExecutionEnvironment:
    return FakeAgentExecutionEnvironment(
        project_path_policy=ProjectPathPolicy(),
    )


class _OpenedSessionContract:
    def __init__(
        self,
        owner: AgentSessionRuntime | FakeAgentSessions,
        sessions: tuple[AgentSession, ...],
    ) -> None:
        self.owner = owner
        self.sessions = sessions

    async def close(self) -> None:
        await self.owner.close()


async def _open_session_contract(
    implementation: str,
    role: AgentRole,
    workspaces: tuple[_Workspace | FakeWorkspace, ...],
    *,
    effects: tuple[Callable[[], None] | None, ...] = (),
    writable_paths: tuple[str, ...] = (),
) -> _OpenedSessionContract:
    selected_effects = effects or (None,) * len(workspaces)
    if implementation == "fake":
        pending_effects = deque(selected_effects)

        def respond(
            _role: AgentRole,
            _history: tuple[str, ...],
            message: str,
            _response: type[BaseModel] | None,
        ) -> object:
            effect = pending_effects.popleft() if pending_effects else None
            if effect is not None:
                effect()
            return message

        owner = FakeAgentSessions((role,), responder=respond)
    else:
        clients = []
        for index, effect in enumerate(selected_effects):
            client = _client(responses=tuple(f"reply-{index}-{turn}" for turn in range(4)))
            if effect is not None:
                client.on_invoke(lambda _call, selected=effect: selected())
            clients.append(client)
        owner = _runtime(
            role,
            _ClientFactory(*clients),
            _EnvironmentOpener(*(_environment() for _workspace_value in workspaces)),
            FakeAgentExecutionLifecycleSink(),
        )
    sessions = tuple(
        [
            await owner.create_session(
                role,
                workspace=workspace,
                writable_paths=writable_paths,
            )
            for workspace in workspaces
        ]
    )
    return _OpenedSessionContract(owner, sessions)


def test_session_fixes_role_binding_and_continues_provider_context() -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    client = _client(responses=("first",)).enqueue("worker", {"value": 2})
    environment = _environment()
    lifecycle = FakeAgentExecutionLifecycleSink()

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _ClientFactory(client),
            _EnvironmentOpener(environment),
            lifecycle,
        )
        session = await runtime.create_session(
            role,
            workspace=_Workspace(),
            member_id="candidate-1",
        )
        assert await session.turn("one") == "first"
        assert await session.turn("two", response=_Reply) == _Reply(value=2)
        assert session.binding.model == "fake-model"
        await runtime.close()

    asyncio.run(scenario())

    calls = client.calls_for("worker")
    assert [call.user_prompt for call in calls] == ["one", "two"]
    assert all(call.system_prompt == "Work carefully." for call in calls)
    assert all(
        call.session_key == AgentSessionKey(SessionScope.MEMBER, "worker:candidate-1")
        for call in calls
    )
    assert all(call.reuse_session is True for call in calls)
    assert client.closed
    assert environment.closed
    assert [type(event) for event in lifecycle.events] == [
        AgentExecutionStarted,
        AgentExecutionFinished,
        AgentExecutionStarted,
        AgentExecutionFinished,
    ]
    assert all(
        event.status is AgentExecutionStatus.COMPLETED
        for event in lifecycle.events
        if isinstance(event, AgentExecutionFinished)
    )


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_same_session_turns_are_serialized(implementation: str) -> None:
    async def scenario() -> None:
        role = AgentRole(id="worker", system_prompt="Work carefully.")
        workspace = (
            _BlockedFakeWorkspace("root")
            if implementation == "fake"
            else _BlockedRuntimeWorkspace("root")
        )
        opened = await _open_session_contract(implementation, role, (workspace,))
        session = opened.sessions[0]
        first = asyncio.create_task(session.turn("first"))
        await workspace.gate.entered.wait()

        queued = asyncio.Event()

        async def second_turn() -> str:
            queued.set()
            return await session.turn("second")

        second = asyncio.create_task(second_turn())
        await queued.wait()
        assert workspace.gate.calls == 1

        workspace.gate.release.set()
        assert await first
        assert await second
        await opened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_different_sessions_can_turn_concurrently(implementation: str) -> None:
    async def scenario() -> None:
        role = AgentRole(id="worker", system_prompt="Work carefully.")
        workspace_type = (
            _BlockedFakeWorkspace if implementation == "fake" else _BlockedRuntimeWorkspace
        )
        first_workspace = workspace_type("first")
        second_workspace = workspace_type("second")
        opened = await _open_session_contract(
            implementation,
            role,
            (first_workspace, second_workspace),
        )
        first = asyncio.create_task(opened.sessions[0].turn("first"))
        second = asyncio.create_task(opened.sessions[1].turn("second"))
        await first_workspace.gate.entered.wait()
        await second_workspace.gate.entered.wait()

        first_workspace.gate.release.set()
        second_workspace.gate.release.set()
        await asyncio.gather(first, second)
        await opened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_close_waits_for_active_turn_and_rejects_queued_and_new_turns(
    implementation: str,
) -> None:
    async def scenario() -> None:
        role = AgentRole(id="worker", system_prompt="Work carefully.")
        workspace = (
            _BlockedFakeWorkspace("root")
            if implementation == "fake"
            else _BlockedRuntimeWorkspace("root")
        )
        opened = await _open_session_contract(implementation, role, (workspace,))
        session = opened.sessions[0]
        active = asyncio.create_task(session.turn("active"))
        await workspace.gate.entered.wait()

        queued_started = asyncio.Event()

        async def queued_turn() -> str:
            queued_started.set()
            return await session.turn("queued")

        queued = asyncio.create_task(queued_turn())
        await queued_started.wait()
        close_started = asyncio.Event()

        async def close_session() -> None:
            close_started.set()
            await session.close()

        closing = asyncio.create_task(close_session())
        await close_started.wait()
        assert session.closed
        assert not closing.done()
        with pytest.raises(SessionClosedError):
            await session.turn("new")

        workspace.gate.release.set()
        assert await active
        with pytest.raises(SessionClosedError):
            await queued
        await closing
        await opened.close()

    asyncio.run(scenario())


_workspace_change_specs = st.lists(
    st.tuples(st.booleans(), st.booleans()),
    min_size=1,
    max_size=6,
)


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("access", [WorkspaceAccess.READ_ONLY, WorkspaceAccess.LIMITED])
@given(specs=_workspace_change_specs)
def test_session_reverts_every_unauthorized_workspace_change(
    implementation: str,
    access: WorkspaceAccess,
    specs: list[tuple[bool, bool]],
) -> None:
    async def scenario() -> None:
        role = AgentRole(id="worker", system_prompt="Work carefully.", workspace_access=access)
        workspace: _Workspace | FakeWorkspace
        workspace = (
            FakeWorkspace(workspace_id="root", path=Path("/root"))
            if implementation == "fake"
            else _Workspace()
        )
        changes: list[str] = []
        grants: list[str] = []
        directories: list[str] = []
        allowed_changes: list[str] = []
        for index, (directory, allowed) in enumerate(specs):
            grant = f"memory-{index}" if directory else f"evidence-{index}.json"
            changed = f"{grant}/report.json" if directory else grant
            changes.append(changed)
            if allowed and access is WorkspaceAccess.LIMITED:
                grants.append(grant)
                allowed_changes.append(changed)
                if directory:
                    directories.append(grant)

        if access is WorkspaceAccess.LIMITED and not grants:
            grants.append("always-allowed.json")
        if isinstance(workspace, FakeWorkspace):
            workspace.declare_directories(*directories)

            def mutate() -> None:
                workspace.script_pending_changes(changes, allowed_changes)

        else:
            workspace.directories.update(directories)

            def mutate() -> None:
                workspace.changes.extend(changes)

        opened = await _open_session_contract(
            implementation,
            role,
            (workspace,),
            effects=(mutate,),
            writable_paths=tuple(grants),
        )
        session = opened.sessions[0]
        await session.turn("write")
        assert session.writable_paths == tuple(grants)
        if isinstance(workspace, FakeWorkspace):
            expected_restores = 1 if set(changes) - set(allowed_changes) else 0
            assert len(workspace.agent_restore_calls) == expected_restores
            expected_revision = "root-revision-2" if allowed_changes else "root-revision-1"
            assert workspace.revision == expected_revision
        else:
            assert workspace.changes == []
            expected_restores = 1 if set(changes) - set(allowed_changes) else 0
            assert len(workspace.agent_restores) == expected_restores
            expected_commit = tuple(allowed_changes) if allowed_changes else ()
            assert workspace.committed_changes[-1] == expected_commit
        await opened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_unrevertable_unauthorized_change_fails_the_turn(implementation: str) -> None:
    class _UnrevertableWorkspace(_Workspace):
        async def restore_for_agent(
            self,
            revision: str,
            *,
            preserve_paths: tuple[str, ...],
        ) -> None:
            self.agent_restores.append((revision, preserve_paths))

    async def scenario() -> None:
        role = AgentRole(
            id="worker",
            system_prompt="Inspect without writing.",
            workspace_access=WorkspaceAccess.READ_ONLY,
        )
        workspace: _UnrevertableWorkspace | FakeWorkspace
        workspace = (
            FakeWorkspace(workspace_id="root", path=Path("/root"))
            if implementation == "fake"
            else _UnrevertableWorkspace()
        )
        if isinstance(workspace, FakeWorkspace):

            def mutate() -> None:
                workspace.script_pending_changes(["unauthorized.txt"], ["unauthorized.txt"])

        else:

            def mutate() -> None:
                workspace.changes.append("unauthorized.txt")

        opened = await _open_session_contract(
            implementation,
            role,
            (workspace,),
            effects=(mutate,),
        )
        with pytest.raises(RuntimeContractError, match=r"unauthorized[.]txt"):
            await opened.sessions[0].turn("inspect")
        await opened.close()

    asyncio.run(scenario())


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
    lifecycle = FakeAgentExecutionLifecycleSink()
    workspace.directories.update(directories)
    client = _client(responses=("done",)).on_invoke(lambda _call: workspace.changes.extend(changes))

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _ClientFactory(client),
            _EnvironmentOpener(_environment()),
            lifecycle,
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


def test_timeout_is_normalized_and_capability_failure_cleans_up() -> None:
    timeout = DriverAgentTurnTimeoutError(4.5)
    role = AgentRole(
        id="worker",
        system_prompt="Work.",
        required_capabilities=frozenset({AgentCapability.PROVIDER_SESSION_RESUME}),
    )
    timed = _client().fail("worker", timeout)
    unsupported = FakeAgentClient(capabilities=AgentCapabilities(session_reuse=True))
    first_environment = _environment()
    rejected_environment = _environment()

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _ClientFactory(timed, unsupported),
            _EnvironmentOpener(first_environment, rejected_environment),
            FakeAgentExecutionLifecycleSink(),
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
    assert rejected_environment.closed


def test_workspace_and_runtime_close_owned_sessions_in_reverse_order() -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    first_client = _client()
    second_client = _client()
    first_environment = _environment()
    second_environment = _environment()
    selected = _Workspace()
    other = _Workspace("other")

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _ClientFactory(first_client, second_client),
            _EnvironmentOpener(first_environment, second_environment),
            FakeAgentExecutionLifecycleSink(),
        )
        selected_session = await runtime.create_session(role, workspace=selected)
        other_session = await runtime.create_session(role, workspace=other)
        await runtime.close_workspace(selected)
        assert selected_session.closed
        assert first_client.closed
        assert first_environment.closed
        assert not other_session.closed
        await runtime.close()
        await runtime.close()
        assert second_client.closed
        assert second_environment.closed
        with pytest.raises(SessionClosedError):
            await runtime.create_session(role, workspace=_Workspace("late"))

    asyncio.run(scenario())


def test_role_tools_are_resolved_once_and_fixed_for_the_session() -> None:
    role = AgentRole(
        id="worker",
        system_prompt="Work.",
        tools=(AgentTool(id="shell"), AgentTool(id="board")),
        required_capabilities=frozenset({AgentCapability.MCP_SERVERS}),
    )
    client = _client(responses=("done",))
    resolutions = 0

    class _Tool:
        name = "board"
        command = "board-server"
        args: tuple[str, ...] = ()
        env: tuple[tuple[str, str], ...] = ()

    def tools(_workspace: Workspace) -> tuple[ToolServerDescriptor, ...]:
        nonlocal resolutions
        resolutions += 1
        return (cast("ToolServerDescriptor", _Tool()),)

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _ClientFactory(client),
            _EnvironmentOpener(_environment()),
            FakeAgentExecutionLifecycleSink(),
            tool_bindings={"board": tools},
        )
        session = await runtime.create_session(role, workspace=_Workspace())
        assert await session.turn("work") == "done"
        await runtime.close()

    asyncio.run(scenario())
    assert resolutions == 1
    assert [tool.name for tool in client.calls_for("worker")[0].tool_servers or []] == ["board"]


def test_environment_client_turn_and_cleanup_share_one_worker_thread() -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    threads: list[tuple[str, int]] = []

    class _ThreadEnvironment(FakeAgentExecutionEnvironment):
        def close(self) -> None:
            threads.append(("close", threading.get_ident()))
            super().close()

    environment = _ThreadEnvironment(project_path_policy=ProjectPathPolicy())

    def open_environment(
        _configuration: AgentExecutionConfiguration,
    ) -> FakeAgentExecutionEnvironment:
        threads.append(("open", threading.get_ident()))
        return environment

    client = _client(responses=("done",)).on_invoke(
        lambda _call: threads.append(("turn", threading.get_ident()))
    )

    async def scenario() -> None:
        runtime = create_agent_session_runtime(
            (role,),
            resolve_execution=lambda selected_role, workspace: (
                AgentExecutionConfiguration(
                    agent_id=selected_role.id,
                    spec=AgentSpec(backend=AgentBackend.STUB),
                ),
                AgentExecutionScope(
                    workspace_path=_workspace(workspace).path,
                    log_directory=Path("/logs"),
                    open_environment=open_environment,
                    current_log_file=StringIO,
                    environment_variables=dict,
                ),
            ),
            resolve_workspace=_workspace,
            session_store=lambda: None,
            control=create_run_control_channel(FakeRunControlEventSink()),
            lifecycle_events=FakeAgentExecutionLifecycleSink(),
            agent_events=NULL_AGENT_EVENT_SINK,
            route_message=lambda message, _steering: message,
            client_factory=_ClientFactory(client),
            log=lambda _message: None,
        )
        session = await runtime.create_session(role, workspace=_Workspace())
        assert await session.turn("work") == "done"
        await runtime.close()

    asyncio.run(scenario())
    assert [name for name, _thread in threads] == ["open", "turn", "close"]
    assert len({thread for _name, thread in threads}) == 1


def test_cancelled_session_close_does_not_cancel_owned_cleanup() -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    close_started = threading.Event()
    release_close = threading.Event()

    class _BlockingEnvironment(FakeAgentExecutionEnvironment):
        def close(self) -> None:
            close_started.set()
            release_close.wait()
            super().close()

    environment = _BlockingEnvironment(project_path_policy=ProjectPathPolicy())

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _ClientFactory(_client()),
            _EnvironmentOpener(environment),
            FakeAgentExecutionLifecycleSink(),
        )
        session = await runtime.create_session(role, workspace=_Workspace())
        waiter = asyncio.create_task(session.close())
        await asyncio.to_thread(close_started.wait)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        release_close.set()
        await session.close()
        assert environment.closed

    asyncio.run(scenario())


def test_cancelled_turn_drains_worker_before_workspace_enforcement() -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    turn_started = threading.Event()
    release_turn = threading.Event()
    workspace = _Workspace()
    lifecycle = FakeAgentExecutionLifecycleSink()

    def block_turn(_call: object) -> None:
        turn_started.set()
        release_turn.wait()

    client = _client(responses=("done",)).on_invoke(block_turn)

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _ClientFactory(client),
            _EnvironmentOpener(_environment()),
            lifecycle,
        )
        session = await runtime.create_session(role, workspace=workspace)
        waiter = asyncio.create_task(session.turn("work"))
        await asyncio.to_thread(turn_started.wait)
        waiter.cancel()
        cancellation_observed = asyncio.get_running_loop().create_future()
        asyncio.get_running_loop().call_soon(cancellation_observed.set_result, None)
        await cancellation_observed
        assert not waiter.done()
        assert workspace.snapshots == ["worker-session-turn-1-input"]
        release_turn.set()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert workspace.snapshots == ["worker-session-turn-1-input"]
        assert isinstance(lifecycle.events[-1], AgentExecutionFinished)
        await runtime.close()

    asyncio.run(scenario())
