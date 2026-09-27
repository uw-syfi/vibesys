"""Public contracts for runtime-owned explicit agent sessions."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import BaseModel
from tests.support.run_execution import run_execution_record

from vs_agent.api import (
    NULL_AGENT_EVENT_SINK,
    AgentBackend,
    AgentCapabilities,
    AgentClient,
    AgentSessionKey,
    AgentSessionState,
    AgentSpec,
    DurableSessionStore,
    SessionScope,
)
from vs_agent.api import AgentTurnTimeoutError as DriverAgentTurnTimeoutError
from vs_agent.api.testing import FakeAgentClient, FakeDriver
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentSession,
    AgentSessions,
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
    BlockingOperations,
    TrustedAccuracyResult,
    TrustedBenchmarkResult,
    WorkspaceEvaluationSpec,
    WorkspaceRuntime,
    create_run_control_channel,
    create_workspace_runtime,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionEnvironment,
    FakeAgentExecutionLifecycleSink,
    FakeAgentSessions,
    FakeRunControlEventSink,
    FakeWorkspace,
)
from vs_sandbox.api import ProjectPathPolicy, SandboxExecutionResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from vs_agent.api import AgentClientProtocol, SessionStore, ToolServerDescriptor


class _Reply(BaseModel):
    value: int


class _WorkspaceResource:
    def __init__(
        self,
        workspace_id: str | None = None,
        *,
        close_events: list[str] | None = None,
    ) -> None:
        self.id = workspace_id
        self.path = Path(f"/{workspace_id or 'root'}")
        self.revision: str | None = "root-revision"
        self.trusted_input_baseline: str | None = "input"
        self.changes: list[str] = []
        self.committed_changes: list[tuple[str, ...]] = []
        self.directories: set[str] = set()
        self.snapshots: list[str] = []
        self.agent_restores: list[tuple[str, tuple[str, ...]]] = []
        self.closed = False
        self._close_events = close_events
        self.scope_factory: Callable[[], AgentExecutionScope] | None = None

    def snapshot(self, label: str) -> str:
        self.snapshots.append(label)
        self.committed_changes.append(tuple(self.changes))
        self.revision = f"revision-{len(self.snapshots)}"
        self.changes.clear()
        return self.revision

    def restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> bool:
        del clean, preserve_memory
        self.revision = revision
        self.agent_restores.append((revision, preserve_paths))
        self.changes = [
            path
            for path in self.changes
            if any(path == allowed or path.startswith(f"{allowed}/") for allowed in preserve_paths)
        ]
        return True

    def try_restore(self, revision: str, *, clean: bool) -> bool:
        return self.restore(revision, clean=clean)

    def retain(self, revision: str, reference: str) -> None:
        del revision, reference

    def pending_changes(self) -> list[str]:
        return list(self.changes)

    def is_directory(self, path: str) -> bool:
        return path in self.directories

    def execute(self, command: str, timeout_seconds: int | None) -> SandboxExecutionResult:
        del command, timeout_seconds
        return SandboxExecutionResult("", 0)

    def agent_scope(self) -> AgentExecutionScope:
        assert self.scope_factory is not None
        return self.scope_factory()

    @property
    def evaluation_spec(self) -> WorkspaceEvaluationSpec:
        return WorkspaceEvaluationSpec(None, None, None, None)

    async def trusted_accuracy(self, command_override: str | None) -> TrustedAccuracyResult:
        return TrustedAccuracyResult(
            command=command_override,
            executed=False,
            passed=True,
        )

    async def trusted_benchmark(
        self,
        command_override: str | None,
        required_metrics: frozenset[str],
    ) -> TrustedBenchmarkResult:
        del required_metrics
        return TrustedBenchmarkResult(
            command=command_override,
            executed=False,
            passed=True,
        )

    def candidate_patch(self, revision: str) -> str:
        return revision

    def trusted_input_changes(self) -> list[str]:
        return []

    def close(self) -> None:
        self.closed = True
        if self._close_events is not None:
            self._close_events.append(f"resource:{self.id or 'root'}")


def _candidate_resource(workspace_id: str, revision: str) -> _WorkspaceResource:
    resource = _WorkspaceResource(workspace_id)
    resource.revision = revision
    return resource


class _SnapshotGate:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def enter(self) -> None:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            self.release.wait()

    async def wait_entered(self) -> None:
        await asyncio.to_thread(self.entered.wait)

    def open(self) -> None:
        self.release.set()


class _AsyncSnapshotGate:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def enter(self) -> None:
        self.calls += 1
        if self.calls == 1:
            self.entered.set()
            await self.release.wait()

    async def wait_entered(self) -> None:
        await self.entered.wait()

    def open(self) -> None:
        self.release.set()


class _BlockedRuntimeWorkspace(_WorkspaceResource):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(workspace_id)
        self.gate = _SnapshotGate()

    def snapshot(self, label: str) -> str:
        self.gate.enter()
        return super().snapshot(label)


class _BlockedFakeWorkspace(FakeWorkspace):
    def __init__(self, workspace_id: str) -> None:
        super().__init__(workspace_id=workspace_id, path=Path(f"/{workspace_id}"))
        self.gate = _AsyncSnapshotGate()

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


def _scope(
    workspace: Workspace,
    opener: _EnvironmentOpener,
) -> AgentExecutionScope:
    return AgentExecutionScope(
        workspace_path=workspace.path,
        log_directory=Path("/logs"),
        open_environment=opener,
        current_log_file=StringIO,
        environment_variables=lambda: {"CUDA_VISIBLE_DEVICES": "2"},
    )


@dataclass(frozen=True, slots=True)
class _RuntimeEffects:
    clients: Callable[..., AgentClientProtocol] | None
    environments: _EnvironmentOpener
    lifecycle: FakeAgentExecutionLifecycleSink
    tool_bindings: dict[str, Callable[[Workspace], tuple[ToolServerDescriptor, ...]]] | None = None


@dataclass(frozen=True, slots=True)
class _WorkspaceResources:
    root: _WorkspaceResource
    create_candidate: Callable[[str, str], _WorkspaceResource]
    supports_parallel_candidates: bool = True


def _runtime(
    role: AgentRole,
    effects: _RuntimeEffects,
    *,
    root_resource: _WorkspaceResource | None = None,
    candidate_resources: tuple[_WorkspaceResource, ...] = (),
) -> WorkspaceRuntime:
    candidates = deque(candidate_resources)
    selected_root = root_resource or _WorkspaceResource()
    selected_root.id = None
    selected_root.scope_factory = lambda: _scope(
        cast("Workspace", selected_root), effects.environments
    )

    def create_candidate(workspace_id: str, revision: str) -> _WorkspaceResource:
        resource = candidates.popleft() if candidates else _WorkspaceResource()
        resource.id = workspace_id
        resource.revision = revision
        resource.scope_factory = lambda: _scope(cast("Workspace", resource), effects.environments)
        return resource

    return create_workspace_runtime(
        (role,),
        workspace_resources=_WorkspaceResources(selected_root, create_candidate),
        resolve_configuration=lambda selected_role: AgentExecutionConfiguration(
            agent_id=selected_role.id,
            spec=AgentSpec(backend=AgentBackend.STUB),
            reasoning_effort="high",
        ),
        session_store=lambda: None,
        control=create_run_control_channel(FakeRunControlEventSink()),
        lifecycle_events=effects.lifecycle,
        agent_events=NULL_AGENT_EVENT_SINK,
        route_message=lambda message, steering: message + "".join(steering),
        blocking=BlockingOperations(),
        client_factory=effects.clients,
        tool_bindings=effects.tool_bindings,
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
        owner: AgentSessions,
        sessions: tuple[AgentSession, ...],
        runtime: WorkspaceRuntime | None = None,
    ) -> None:
        self.owner = owner
        self.sessions = sessions
        self.runtime = runtime

    async def close(self) -> None:
        if self.runtime is None:
            await self.owner.close()
        else:
            await self.runtime.workspaces.close()


async def _open_session_contract(
    implementation: str,
    role: AgentRole,
    workspaces: tuple[_WorkspaceResource | FakeWorkspace, ...],
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

        owner: AgentSessions = FakeAgentSessions((role,), responder=respond)
        runtime = None
        fake_workspaces = tuple(
            workspace for workspace in workspaces if isinstance(workspace, FakeWorkspace)
        )
        if len(fake_workspaces) != len(workspaces):
            pytest.fail("fake contract requires fake workspaces")
        session_workspaces: tuple[Workspace, ...] = fake_workspaces
    else:
        clients = []
        for index, effect in enumerate(selected_effects):
            client = _client(responses=tuple(f"reply-{index}-{turn}" for turn in range(4)))
            if effect is not None:
                client.on_invoke(lambda _call, selected=effect: selected())
            clients.append(client)
        resources = tuple(
            resource for resource in workspaces if isinstance(resource, _WorkspaceResource)
        )
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(*clients),
                _EnvironmentOpener(*(_environment() for _workspace_value in workspaces)),
                FakeAgentExecutionLifecycleSink(),
            ),
            root_resource=resources[0],
            candidate_resources=resources[1:],
        )
        owner = runtime.agents
        handles = [runtime.workspaces.root]
        handles.extend([await runtime.workspaces.create_candidate() for _resource in resources[1:]])
        session_workspaces = tuple(handles)
    sessions = tuple(
        [
            await owner.create_session(
                role,
                workspace=workspace,
                writable_paths=writable_paths,
            )
            for workspace in session_workspaces
        ]
    )
    return _OpenedSessionContract(owner, sessions, runtime)


def test_session_fixes_role_binding_and_continues_provider_context() -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    client = _client(responses=("first",)).enqueue("worker", {"value": 2})
    environment = _environment()
    lifecycle = FakeAgentExecutionLifecycleSink()

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(_ClientFactory(client), _EnvironmentOpener(environment), lifecycle),
        )
        session = await runtime.agents.create_session(
            role,
            workspace=runtime.workspaces.root,
            member_id="candidate-1",
        )
        assert await session.turn("one") == "first"
        assert await session.turn("two", response=_Reply) == _Reply(value=2)
        assert session.binding.model == "fake-model"
        await runtime.workspaces.close()

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


def test_workspace_runtime_owns_the_default_agent_client_factory() -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.")

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(
                None,
                _EnvironmentOpener(_environment()),
                FakeAgentExecutionLifecycleSink(),
            ),
        )
        session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
        assert await session.turn("inspect") == (
            "Stub agent inspected the available experiment trajectory."
        )
        await runtime.workspaces.close()

    asyncio.run(scenario())


def test_named_session_resumes_provider_context_after_runtime_reopens(tmp_path: Path) -> None:
    project = Project.open(tmp_path)
    project.state.create_project("runtime session continuity")
    manifest = project.state.new_run_manifest(
        "runtime session continuity",
        run_id="run-1",
        trusted_input_baseline="a" * 40,
        branch="vibesys/run-1",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
    )
    project.state.create_run(manifest)
    slot = project.state.local_namespace(manifest.run_id, "agent").slot(
        "sessions.json",
        AgentSessionState,
    )
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    session_key = AgentSessionKey(SessionScope.MEMBER, "worker:candidate-1")
    drivers: list[FakeDriver] = []

    def open_client(**kwargs: object) -> AgentClient:
        store = cast("SessionStore | None", kwargs["session_store"])
        driver = FakeDriver(answer="done")
        drivers.append(driver)
        return AgentClient(
            driver,
            provider="fake",
            model_name="fake-model",
            driver_name="fake",
            session_store=store,
        )

    async def run_once() -> str:
        clients: list[AgentClient] = []

        def record_client(**kwargs: object) -> AgentClient:
            client = open_client(**kwargs)
            clients.append(client)
            return client

        environments = _EnvironmentOpener(_environment())
        root_resource = _WorkspaceResource()
        root_resource.scope_factory = lambda: _scope(cast("Workspace", root_resource), environments)
        runtime = create_workspace_runtime(
            (role,),
            workspace_resources=_WorkspaceResources(root_resource, _candidate_resource),
            resolve_configuration=lambda selected_role: AgentExecutionConfiguration(
                agent_id=selected_role.id,
                spec=AgentSpec(backend=AgentBackend.STUB),
                reasoning_effort="high",
            ),
            session_store=lambda: DurableSessionStore(slot),
            control=create_run_control_channel(FakeRunControlEventSink()),
            lifecycle_events=FakeAgentExecutionLifecycleSink(),
            agent_events=NULL_AGENT_EVENT_SINK,
            route_message=lambda message, steering: message + "".join(steering),
            blocking=BlockingOperations(),
            client_factory=record_client,
            log=lambda _message: None,
        )
        session = await runtime.agents.create_session(
            role,
            workspace=runtime.workspaces.root,
            member_id="candidate-1",
        )
        assert await session.turn("continue") == "done"
        provider_session_id = clients[0].last_turn_provider_session_id(session_key)
        assert provider_session_id is not None
        await runtime.workspaces.close()
        return provider_session_id

    first_provider_session = asyncio.run(run_once())
    resumed_provider_session = asyncio.run(run_once())

    assert resumed_provider_session == first_provider_session
    assert drivers[0].resumed_session_ids == ()
    assert drivers[1].resumed_session_ids == (first_provider_session,)


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
        await workspace.gate.wait_entered()

        queued = asyncio.Event()

        async def second_turn() -> str:
            queued.set()
            return await session.turn("second")

        second = asyncio.create_task(second_turn())
        await queued.wait()
        assert workspace.gate.calls == 1

        workspace.gate.open()
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
        await first_workspace.gate.wait_entered()
        await second_workspace.gate.wait_entered()

        first_workspace.gate.open()
        second_workspace.gate.open()
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
        await workspace.gate.wait_entered()

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

        workspace.gate.open()
        assert await active
        with pytest.raises(SessionClosedError):
            await queued
        await closing
        await opened.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_cancelled_close_preserves_owned_cleanup_for_a_later_waiter(
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
        await workspace.gate.wait_entered()

        close_started = asyncio.Event()

        async def close_session() -> None:
            close_started.set()
            await session.close()

        closing = asyncio.create_task(close_session())
        await close_started.wait()
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing

        workspace.gate.open()
        assert await active
        await session.close()
        assert session.closed
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
        workspace: _WorkspaceResource | FakeWorkspace
        workspace = (
            FakeWorkspace(workspace_id="root", path=Path("/root"))
            if implementation == "fake"
            else _WorkspaceResource()
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
    class _UnrevertableWorkspace(_WorkspaceResource):
        def restore(
            self,
            revision: str,
            *,
            clean: bool,
            preserve_paths: tuple[str, ...] = (),
            preserve_memory: bool = True,
        ) -> bool:
            del clean, preserve_memory
            self.agent_restores.append((revision, preserve_paths))
            self.revision = revision
            return True

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
    workspace = _WorkspaceResource()
    lifecycle = FakeAgentExecutionLifecycleSink()
    workspace.directories.update(directories)
    client = _client(responses=("done",)).on_invoke(lambda _call: workspace.changes.extend(changes))

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(_ClientFactory(client), _EnvironmentOpener(_environment()), lifecycle),
            root_resource=workspace,
        )
        session = await runtime.agents.create_session(
            role,
            workspace=runtime.workspaces.root,
            writable_paths=grants,
        )
        assert await session.turn("work") == "done"
        assert workspace.changes == []
        assert workspace.committed_changes[-1] == tuple(remaining)
        await runtime.workspaces.close()

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
            _RuntimeEffects(
                _ClientFactory(timed, unsupported),
                _EnvironmentOpener(first_environment, rejected_environment),
                FakeAgentExecutionLifecycleSink(),
            ),
        )
        session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
        with pytest.raises(AgentTurnTimeoutError) as raised:
            await session.turn("work")
        assert raised.value.__cause__ is timeout
        with pytest.raises(RuntimeContractError, match="provider_session_resume"):
            await runtime.agents.create_session(
                role, workspace=runtime.workspaces.root, member_id="durable"
            )
        await runtime.workspaces.close()

    asyncio.run(scenario())
    assert unsupported.closed
    assert rejected_environment.closed


def test_session_creation_rejects_foreign_and_discarded_workspace_handles() -> None:
    role = AgentRole(id="worker", system_prompt="Work.")

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(),
                _EnvironmentOpener(),
                FakeAgentExecutionLifecycleSink(),
            ),
        )
        with pytest.raises(TypeError, match="live handle from this run"):
            await runtime.agents.create_session(
                role,
                workspace=FakeWorkspace(path=Path("/foreign")),
            )
        candidate = await runtime.workspaces.create_candidate()
        await candidate.discard()
        with pytest.raises(ValueError, match="workspace is closed"):
            await runtime.agents.create_session(role, workspace=candidate)
        await runtime.workspaces.close()

    asyncio.run(scenario())


def test_workspace_and_runtime_close_owned_sessions_in_reverse_order() -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    first_client = _client()
    second_client = _client()
    events: list[str] = []

    class _OrderedEnvironment(FakeAgentExecutionEnvironment):
        def __init__(self, label: str) -> None:
            super().__init__(project_path_policy=ProjectPathPolicy())
            self._label = label

        def close(self) -> None:
            events.append(f"session:{self._label}")
            super().close()

    first_environment = _OrderedEnvironment("root")
    second_environment = _OrderedEnvironment("candidate")
    root_resource = _WorkspaceResource(close_events=events)
    candidate_resource = _WorkspaceResource("candidate", close_events=events)

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(first_client, second_client),
                _EnvironmentOpener(first_environment, second_environment),
                FakeAgentExecutionLifecycleSink(),
            ),
            root_resource=root_resource,
            candidate_resources=(candidate_resource,),
        )
        candidate = await runtime.workspaces.create_candidate()
        candidate_id = candidate.id
        root_session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
        candidate_session = await runtime.agents.create_session(role, workspace=candidate)
        await candidate.discard()
        assert candidate_session.closed
        assert second_client.closed
        assert second_environment.closed
        assert candidate_resource.closed
        assert not root_session.closed
        await runtime.workspaces.close()
        await runtime.workspaces.close()
        assert first_client.closed
        assert first_environment.closed
        assert root_resource.closed
        assert events == [
            "session:candidate",
            f"resource:{candidate_id}",
            "session:root",
            "resource:root",
        ]
        with pytest.raises(SessionClosedError):
            await runtime.agents.create_session(role, workspace=runtime.workspaces.root)

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
            _RuntimeEffects(
                _ClientFactory(client),
                _EnvironmentOpener(_environment()),
                FakeAgentExecutionLifecycleSink(),
                tool_bindings={"board": tools},
            ),
        )
        session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
        assert await session.turn("work") == "done"
        await runtime.workspaces.close()

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
        root_resource = _WorkspaceResource()
        root_resource.scope_factory = lambda: AgentExecutionScope(
            workspace_path=root_resource.path,
            log_directory=Path("/logs"),
            open_environment=open_environment,
            current_log_file=StringIO,
            environment_variables=dict,
        )
        runtime = create_workspace_runtime(
            (role,),
            workspace_resources=_WorkspaceResources(root_resource, _candidate_resource),
            resolve_configuration=lambda selected_role: AgentExecutionConfiguration(
                agent_id=selected_role.id,
                spec=AgentSpec(backend=AgentBackend.STUB),
            ),
            session_store=lambda: None,
            control=create_run_control_channel(FakeRunControlEventSink()),
            lifecycle_events=FakeAgentExecutionLifecycleSink(),
            agent_events=NULL_AGENT_EVENT_SINK,
            route_message=lambda message, _steering: message,
            blocking=BlockingOperations(),
            client_factory=_ClientFactory(client),
            log=lambda _message: None,
        )
        session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
        assert await session.turn("work") == "done"
        await runtime.workspaces.close()

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
            _RuntimeEffects(
                _ClientFactory(_client()),
                _EnvironmentOpener(environment),
                FakeAgentExecutionLifecycleSink(),
            ),
        )
        session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
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
    workspace = _WorkspaceResource()
    lifecycle = FakeAgentExecutionLifecycleSink()

    def block_turn(_call: object) -> None:
        turn_started.set()
        release_turn.wait()

    client = _client(responses=("done",)).on_invoke(block_turn)

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(_ClientFactory(client), _EnvironmentOpener(_environment()), lifecycle),
            root_resource=workspace,
        )
        session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
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
        await runtime.workspaces.close()

    asyncio.run(scenario())
