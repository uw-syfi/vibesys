"""Shared public-API fixtures for runtime agent session contract suites."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from tests.support.run_execution import run_execution_record

from vs_agent.api import (
    NULL_AGENT_EVENT_SINK,
    AgentBackend,
    AgentCapabilities,
    AgentClient,
    AgentExecutionPolicy,
    AgentInvocationState,
    AgentSessionKey,
    AgentSessionSpec,
    AgentSessionState,
    AgentSpec,
    AgentTurnRequest,
    DurableSessionStore,
    SessionScope,
)
from vs_agent.api.testing import FakeAgentClient, FakeAgentSessions, FakeDriver
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentSession,
    AgentToolBindingContext,
    Workspace,
    WorkspaceAgentSessions,
)
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionScope,
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
    FakeRunControlEventSink,
    FakeWorkspace,
    FakeWorkspaceAgentSessions,
)
from vs_sandbox.api import ProjectPathPolicy, SandboxExecutionResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import BaseModel

    from vs_agent.api import (
        AgentClientProtocol,
        AgentInvocationStore,
        AgentSessions,
        ToolServerDescriptor,
    )
    from vs_project.api import StateSlot


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

    def has_revision(self, revision: str) -> bool:
        del revision
        return True

    def matches_revision(self, revision: str) -> bool:
        del revision
        return True

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
    tool_bindings: (
        dict[str, Callable[[AgentToolBindingContext], tuple[ToolServerDescriptor, ...]]] | None
    ) = None
    session_transport: AgentSessions | None = None
    invocation_store: Callable[[AgentSessionKey], AgentInvocationStore] | None = None


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
        resource = candidates.popleft() if candidates else _WorkspaceResource(workspace_id)
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
        session_transport=effects.session_transport,
        invocation_store=effects.invocation_store,
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
        owner: WorkspaceAgentSessions,
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

        owner: WorkspaceAgentSessions = FakeWorkspaceAgentSessions((role,), responder=respond)
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


def _durable_session_slot(tmp_path: Path) -> StateSlot[AgentSessionState]:
    project = Project.open(tmp_path)
    project.state.create_project("member workspace continuity")
    manifest = project.state.new_run_manifest(
        "member workspace continuity",
        run_id="run-1",
        trusted_input_baseline="a" * 40,
        branch="vibesys/run-1",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
    )
    project.state.create_run(manifest)
    return project.state.local_namespace(manifest.run_id, "agent").slot(
        "sessions.json",
        AgentSessionState,
    )


async def _open_resume_contract(
    implementation: str,
    role: AgentRole,
    workspace: _WorkspaceResource | FakeWorkspace,
    transport: AgentSessions,
    grants: tuple[str, ...] = (),
) -> _OpenedSessionContract:
    if implementation == "fake":
        owner = FakeWorkspaceAgentSessions(
            (role,),
            supported_agent_capabilities=(
                AgentCapability.SESSION_REUSE,
                AgentCapability.PROVIDER_SESSION_RESUME,
            ),
        )
        owner.bind_session_transport(transport)
        runtime = None
        assert isinstance(workspace, FakeWorkspace)
        handle: Workspace = workspace
    else:
        assert isinstance(workspace, _WorkspaceResource)
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(_client(), _client()),
                _EnvironmentOpener(_environment(), _environment()),
                FakeAgentExecutionLifecycleSink(),
                session_transport=transport,
            ),
            root_resource=workspace,
        )
        owner = runtime.agents
        handle = runtime.workspaces.root
    session = await owner.create_session(
        role, workspace=handle, member_id="member", writable_paths=grants
    )
    return _OpenedSessionContract(owner, (session,), runtime)


def _resume_transport(
    tmp_path: Path, before_turn: Callable[[AgentTurnRequest], None]
) -> tuple[FakeAgentSessions, AgentClient]:
    slot = _durable_session_slot(tmp_path)
    client = AgentClient(
        FakeDriver(answer="done", on_turn=before_turn),
        session_store=DurableSessionStore(slot),
    )
    key = AgentSessionKey(SessionScope.MEMBER, "worker:member")
    spec = AgentSessionSpec(
        role="worker", provider="fake", workspace=tmp_path, policy=AgentExecutionPolicy()
    )
    client.run(session_spec=spec, turn=AgentTurnRequest(message="first"), session_key=key)
    ledger = (
        Project.open(tmp_path)
        .state.local_namespace("run-1", "agent")
        .slot("invocations.json", AgentInvocationState)
    )
    transport = FakeAgentSessions(client, ledger)
    transport.bind(key, spec, AgentTurnRequest(message="template"))
    return transport, client
