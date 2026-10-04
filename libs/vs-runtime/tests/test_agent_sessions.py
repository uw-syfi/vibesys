"""Public contracts for runtime-owned explicit agent sessions."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from pydantic import BaseModel, TypeAdapter, ValidationError
from tests.support.run_execution import run_execution_record
from tests.support.runtime_agent_sessions import (
    _BlockedFakeWorkspace,
    _BlockedRuntimeWorkspace,
    _candidate_resource,
    _client,
    _ClientFactory,
    _durable_session_slot,
    _environment,
    _EnvironmentOpener,
    _open_resume_contract,
    _open_session_contract,
    _resume_transport,
    _runtime,
    _RuntimeEffects,
    _scope,
    _WorkspaceResource,
    _WorkspaceResources,
)

from vs_agent.api import (
    NULL_AGENT_EVENT_SINK,
    AgentBackend,
    AgentCapabilities,
    AgentClient,
    AgentOutputSchemaError,
    AgentSessionKey,
    AgentSessionState,
    AgentSpec,
    AgentTurnRequest,
    Completed,
    DurableSessionStore,
    SessionScope,
    Unknown,
)
from vs_agent.api import AgentTurnTimeoutError as DriverAgentTurnTimeoutError
from vs_agent.api.testing import FakeAgentClient, FakeDriver
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import (
    AgentCapability,
    AgentId,
    AgentRole,
    AgentTool,
    AgentToolBindingContext,
    AgentTurnTimeoutError,
    RuntimeContractError,
    SessionClosedError,
    SessionTransportUnavailableError,
    StructuredResponseError,
    Workspace,
    WorkspaceAccess,
    WorkspaceRestoreError,
)
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionFinished,
    AgentExecutionScope,
    AgentExecutionStarted,
    AgentExecutionStatus,
    BlockingOperations,
    create_run_control_channel,
    create_workspace_runtime,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionEnvironment,
    FakeAgentExecutionLifecycleSink,
    FakeAgentSession,
    FakeRunControlEventSink,
    FakeWorkspace,
    FakeWorkspaceAgentSessions,
    FakeWorkspaces,
)
from vs_sandbox.api import ProjectPathPolicy

if TYPE_CHECKING:
    from vs_agent.api import AgentClientProtocol, SessionStore, ToolServerDescriptor


class _Reply(BaseModel):
    value: int


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


@pytest.mark.parametrize(
    ("member_id", "resumes"),
    [("h-batched-decode", True), (None, False)],
    ids=["member-keyed", "unkeyed"],
)
def test_member_keyed_candidate_resumes_its_provider_session_from_a_new_revision(
    tmp_path: Path,
    member_id: str | None,
    *,
    resumes: bool,
) -> None:
    slot = _durable_session_slot(tmp_path)
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    session_key = AgentSessionKey(SessionScope.MEMBER, "worker:h-batched-decode")
    drivers: list[FakeDriver] = []
    clients: list[AgentClient] = []

    def open_client(**kwargs: object) -> AgentClient:
        driver = FakeDriver(answer="done")
        drivers.append(driver)
        client = AgentClient(
            driver,
            provider="fake",
            model_name="fake-model",
            driver_name="fake",
            session_store=cast("SessionStore | None", kwargs["session_store"]),
        )
        clients.append(client)
        return client

    environments = _EnvironmentOpener(_environment(), _environment())

    def create_candidate(workspace_id: str, revision: str) -> _WorkspaceResource:
        resource = _candidate_resource(workspace_id, revision)
        resource.scope_factory = lambda: _scope(cast("Workspace", resource), environments)
        return resource

    root_resource = _WorkspaceResource()

    async def scenario() -> list[Path]:
        runtime = create_workspace_runtime(
            (role,),
            workspace_resources=_WorkspaceResources(root_resource, create_candidate),
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
            client_factory=open_client,
            log=lambda _message: None,
        )
        paths: list[Path] = []
        # Two successive attempts of one member, each from a new parent.
        for parent in ("parent-1", "parent-2"):
            candidate = await runtime.workspaces.create_candidate(parent, member_id=member_id)
            paths.append(candidate.path)
            session = await runtime.agents.create_session(
                role,
                workspace=candidate,
                member_id="h-batched-decode",
            )
            assert await session.turn(f"attempt from {parent}") == "done"
            await candidate.discard()
        await runtime.workspaces.close()
        return paths

    first_path, second_path = asyncio.run(scenario())

    first_session = clients[0].last_turn_provider_session_id(session_key)
    assert first_session is not None
    assert drivers[0].resumed_session_ids == ()
    if resumes:
        assert first_path == second_path
        assert drivers[1].resumed_session_ids == (first_session,)
        assert clients[1].last_turn_provider_session_id(session_key) == first_session
    else:
        assert first_path != second_path
        assert drivers[1].resumed_session_ids == ()


def test_member_keyed_candidates_are_isolated_and_exclusive() -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.")

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(None, _EnvironmentOpener(), FakeAgentExecutionLifecycleSink()),
        )
        first = await runtime.workspaces.create_candidate(member_id="h1")
        second = await runtime.workspaces.create_candidate(member_id="h2")
        assert first.path != second.path
        with pytest.raises(RuntimeContractError, match="already has a live candidate"):
            await runtime.workspaces.create_candidate(member_id="h1")
        first_path = first.path
        await first.discard()
        again = await runtime.workspaces.create_candidate(member_id="h1")
        assert again.path == first_path
        await runtime.workspaces.close()

    asyncio.run(scenario())


def test_fake_member_session_resumes_only_from_the_same_workspace_path() -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    sessions = FakeWorkspaceAgentSessions(
        (role,),
        supported_agent_capabilities={
            AgentCapability.SESSION_REUSE,
            AgentCapability.PROVIDER_SESSION_RESUME,
        },
    )
    root = FakeWorkspace(path=Path("/project"))
    workspaces = FakeWorkspaces(root, supports_parallel_candidates=True, sessions=sessions)

    async def scenario() -> tuple[tuple[str, ...], tuple[str, ...]]:
        first = await workspaces.create_candidate(member_id="h1")
        session = await sessions.create_session(role, workspace=first, member_id="h1")
        await session.turn("first attempt")
        await first.discard()
        resumed_workspace = await workspaces.create_candidate(member_id="h1")
        resumed = await sessions.create_session(role, workspace=resumed_workspace, member_id="h1")
        fresh_workspace = await workspaces.create_candidate()
        fresh = await sessions.create_session(role, workspace=fresh_workspace, member_id="h1")
        return cast("FakeAgentSession", resumed).history, cast("FakeAgentSession", fresh).history

    resumed_history, fresh_history = asyncio.run(scenario())

    assert resumed_history == ("first attempt",)
    assert fresh_history == ()


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


@given(detail=st.text(min_size=1).filter(str.strip))
def test_a_provider_schema_failure_is_a_structured_response_error_in_the_same_session(
    detail: str,
) -> None:
    """Regression: r10's planner exhausted the provider's schema retries and the run ended.

    The failure reaches plugin code as the same error as an unparseable reply,
    carrying the validation errors, and the next turn continues the session.
    """
    role = AgentRole(id="worker", system_prompt="Work.")
    failure = AgentOutputSchemaError(detail)
    client = _client().fail("worker", failure, times=1).enqueue("worker", {"value": 2})

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(client),
                _EnvironmentOpener(_environment()),
                FakeAgentExecutionLifecycleSink(),
            ),
        )
        session = await runtime.agents.create_session(
            role, workspace=runtime.workspaces.root, member_id="m"
        )
        with pytest.raises(StructuredResponseError) as raised:
            await session.turn("work", response=_Reply)
        assert raised.value.detail == detail
        assert detail in str(raised.value)
        assert raised.value.__cause__ is failure
        assert await session.turn("correct it", response=_Reply) == _Reply(value=2)
        await runtime.workspaces.close()

    asyncio.run(scenario())
    first, second = client.calls_for("worker")
    assert first.session_key == second.session_key


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


def test_role_extra_tools_are_resolved_once_and_fixed_for_the_session() -> None:
    role = AgentRole(
        id="worker",
        system_prompt="Work.",
        extra_tools=(AgentTool(id="board"),),
        required_capabilities=frozenset({AgentCapability.MCP_SERVERS}),
    )
    client = _client(responses=("done",))
    resolutions = 0

    class _Tool:
        name = "board"
        command = "board-server"
        args: tuple[str, ...] = ()

        def __init__(self, path: str) -> None:
            self.env = (("SOCKET", path),)

    class _TranslatedEnvironment(FakeAgentExecutionEnvironment):
        def agent_path(self, host_path: Path | str) -> str:
            return f"/agent{host_path}"

    observed: list[tuple[AgentToolBindingContext, str | None]] = []

    def tools(context: AgentToolBindingContext) -> tuple[ToolServerDescriptor, ...]:
        nonlocal resolutions
        resolutions += 1
        observed.append((context, context.workspace.id))
        return (cast("ToolServerDescriptor", _Tool(context.agent_path(Path("/host.sock")))),)

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(client),
                _EnvironmentOpener(_TranslatedEnvironment(project_path_policy=ProjectPathPolicy())),
                FakeAgentExecutionLifecycleSink(),
                tool_bindings={"board": tools},
            ),
        )
        session = await runtime.agents.create_session(
            role, workspace=runtime.workspaces.root, member_id="hypothesis-1"
        )
        assert await session.turn("work") == "done"
        await runtime.workspaces.close()

    asyncio.run(scenario())
    assert resolutions == 1
    context, workspace_id = observed[0]
    assert context.role is role
    assert workspace_id is None
    assert context.member_id == "hypothesis-1"
    bound_tools = client.calls_for("worker")[0].tool_servers or []
    assert [tool.name for tool in bound_tools] == ["board"]
    assert dict(bound_tools[0].env)["SOCKET"] == "/agent/host.sock"


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


def test_begin_close_propagates_cancellation_to_agent_clients() -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    client = _client()

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(client),
                _EnvironmentOpener(_environment()),
                FakeAgentExecutionLifecycleSink(),
            ),
        )
        await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
        runtime.workspaces.begin_close()
        assert client.cancel_count == 1
        await runtime.workspaces.close()

    asyncio.run(scenario())


@pytest.fixture(scope="module")
def state_project(tmp_path_factory: pytest.TempPathFactory) -> tuple[Project, str]:
    root = tmp_path_factory.mktemp("member-namespaces")
    (root / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    project = Project.open(root)
    project.state.create_project("members", now=datetime(2026, 8, 11, tzinfo=UTC))
    manifest = project.state.new_run_manifest(
        "members",
        branch="vibesys/members",
        vibesys_version="0.2.0",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="multi-agent", config_version=1, options={}),
        trusted_input_baseline="a" * 40,
        now=datetime(2026, 8, 11, tzinfo=UTC),
    )
    project.state.create_run(manifest)
    return project, manifest.run_id


GIT = shutil.which("git") or "git"
_DOTTED_MEMBER_ID = "KV.Cache_v2 / ../Ünïcode"


def _is_agent_id(value: str) -> bool:
    """Whether ``value`` is a member ID an agent is allowed to supply."""
    try:
        TypeAdapter(AgentId).validate_python(value)
    except ValidationError:
        return False
    return True


def _git_accepts_candidate_ref(workspace_id: str) -> bool:
    ref = f"refs/vibesys/run/candidates/{workspace_id}"
    # lint-waiver: LW-994301 [S603]; the test runs Git's own ref-name validator on a
    # generated ref with a fixed argv and no shell.
    # > A wrapper only moves this call, and a reimplementation of the rules in
    # > Python could drift from what Git accepts.
    result = subprocess.run(  # noqa: S603
        [GIT, "check-ref-format", ref], check=False, capture_output=True
    )
    return result.returncode == 0


@example(member_ids=[_DOTTED_MEMBER_ID])
@example(member_ids=["v1..v2", "v1.-v2", "x.lock", "x.", ".x", "a@{b"])
@given(
    member_ids=st.lists(
        st.text(st.characters(blacklist_categories=("Cc", "Cs")), min_size=1, max_size=128).filter(
            _is_agent_id
        ),
        min_size=1,
        max_size=6,
        unique=True,
    )
)
def test_member_candidate_ids_are_distinct_valid_state_namespaces_and_git_refs(
    member_ids: list[str], state_project: tuple[Project, str]
) -> None:
    project, run_id = state_project
    role = AgentRole(id="worker", system_prompt="Work carefully.")

    async def scenario() -> list[str]:
        runtime = _runtime(
            role,
            _RuntimeEffects(None, _EnvironmentOpener(), FakeAgentExecutionLifecycleSink()),
        )
        ids: list[str] = []
        for member_id in member_ids:
            candidate = await runtime.workspaces.create_candidate(member_id=member_id)
            assert candidate.id is not None
            ids.append(candidate.id)
        await runtime.workspaces.close()
        return ids

    workspace_ids = asyncio.run(scenario())

    for workspace_id in workspace_ids:
        project.state.local_namespace(run_id, workspace_id)
        assert _git_accepts_candidate_ref(workspace_id), workspace_id
    assert len(set(workspace_ids)) == len(member_ids)


def test_dotted_unicode_member_id_opens_a_candidate_with_a_valid_git_ref_name() -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.")

    async def scenario() -> str | None:
        runtime = _runtime(
            role,
            _RuntimeEffects(None, _EnvironmentOpener(), FakeAgentExecutionLifecycleSink()),
        )
        candidate = await runtime.workspaces.create_candidate(member_id=_DOTTED_MEMBER_ID)
        workspace_id = candidate.id
        await runtime.workspaces.close()
        return workspace_id

    workspace_id = asyncio.run(scenario())
    assert workspace_id is not None
    assert _git_accepts_candidate_ref(workspace_id), workspace_id


def test_uppercase_member_ids_stay_distinct_from_lowercase_ones() -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.")

    async def scenario() -> tuple[str | None, str | None]:
        runtime = _runtime(
            role,
            _RuntimeEffects(None, _EnvironmentOpener(), FakeAgentExecutionLifecycleSink()),
        )
        upper = await runtime.workspaces.create_candidate(member_id="H1")
        lower = await runtime.workspaces.create_candidate(member_id="h1")
        ids = (upper.id, lower.id)
        await runtime.workspaces.close()
        return ids

    upper_id, lower_id = asyncio.run(scenario())
    assert upper_id is not None
    assert upper_id.startswith("m-h1-")
    assert upper_id != lower_id


@given(error_number=st.integers(min_value=1, max_value=133))
def test_session_spawn_os_errors_are_typed_and_retryable(error_number: int) -> None:
    failure = OSError(error_number, "agent helper cannot execute")
    role = AgentRole(id="worker", system_prompt="Work.")
    client = _client(responses=("recovered",)).fail("worker", failure, times=1)

    async def scenario() -> None:
        runtime = _runtime(
            role,
            _RuntimeEffects(
                _ClientFactory(client),
                _EnvironmentOpener(_environment()),
                FakeAgentExecutionLifecycleSink(),
            ),
        )
        session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
        try:
            with pytest.raises(RuntimeError, match="could not start") as raised:
                await session.turn("work")
            assert type(raised.value).__name__ == "AgentSpawnError"
            assert isinstance(raised.value.__cause__, OSError)
            assert raised.value.__cause__.errno == error_number
            assert getattr(raised.value, "retryable", False)
            assert await session.turn("retry") == "recovered"
        finally:
            await runtime.workspaces.close()

    asyncio.run(scenario())


@given(
    failure=st.one_of(
        st.integers(min_value=1, max_value=133).map(
            lambda number: OSError(number, "agent factory cannot execute")
        ),
        st.just(ImportError("vendored provider helper missing")),
    )
)
def test_session_factory_spawn_faults_release_environment_and_allow_retry(
    failure: OSError | ImportError,
) -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    client = _client(responses=("recovered",))

    class FailingFactory(_ClientFactory):
        def __call__(self, **kwargs: object) -> AgentClientProtocol:
            if not self.calls:
                self.calls.append(kwargs)
                raise failure
            return super().__call__(**kwargs)

    async def scenario() -> None:
        failed_environment = _environment()
        runtime = _runtime(
            role,
            _RuntimeEffects(
                FailingFactory(client),
                _EnvironmentOpener(failed_environment, _environment()),
                FakeAgentExecutionLifecycleSink(),
            ),
        )
        try:
            with pytest.raises(RuntimeError, match="could not start") as raised:
                await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
            assert type(raised.value).__name__ == "AgentSpawnError"
            assert getattr(raised.value, "retryable", False)
            assert raised.value.__cause__ is failure
            assert failed_environment.closed
            session = await runtime.agents.create_session(role, workspace=runtime.workspaces.root)
            assert await session.turn("retry") == "recovered"
        finally:
            await runtime.workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_resume_requires_explicit_transport(implementation: str, tmp_path: Path) -> None:
    """Legacy turns do not become an implicit fresh-session resume fallback."""
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    workspace = FakeWorkspace() if implementation == "fake" else _WorkspaceResource()
    message = TemplateRenderer(tmp_path).render_string("trusted result")

    async def check() -> None:
        opened = await _open_session_contract(implementation, role, (workspace,))
        session = opened.sessions[0]
        try:
            with pytest.raises(SessionTransportUnavailableError):
                session.checkpoint()
            with pytest.raises(SessionTransportUnavailableError):
                session.inspect("resume-id")
            with pytest.raises(SessionTransportUnavailableError):
                await session.resume(message, "resume-id")
            assert await session.turn("ordinary turn")
            await session.close()
            with pytest.raises(SessionClosedError):
                await session.resume(message, "resume-after-close")
        finally:
            await opened.close()

    asyncio.run(check())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("access", [WorkspaceAccess.READ_ONLY, WorkspaceAccess.LIMITED])
def test_resume_preserves_checkpoint_outcome_and_workspace_grants(
    implementation: str, access: WorkspaceAccess, tmp_path: Path
) -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.", workspace_access=access)
    workspace = FakeWorkspace() if implementation == "fake" else _WorkspaceResource()
    grants = ("allowed.json",) if access is WorkspaceAccess.LIMITED else ()
    expected = ["allowed.json"] if grants else []
    calls: list[str] = []

    def mutate(request: AgentTurnRequest) -> None:
        if request.invocation_id is None:
            return
        calls.append(request.invocation_id)
        if isinstance(workspace, FakeWorkspace):
            workspace.script_pending_changes(["allowed.json", "forbidden.py"], expected)
        else:
            workspace.changes.extend(["allowed.json", "forbidden.py"])

    transport, client = _resume_transport(tmp_path, mutate)
    message = TemplateRenderer(tmp_path).render_string("trusted result")

    async def check() -> None:
        opened = await _open_resume_contract(implementation, role, workspace, transport, grants)
        session = opened.sessions[0]
        try:
            before = session.checkpoint()
            outcome = await session.resume(message, "resume-id")
            assert isinstance(outcome, Completed)
            assert session.checkpoint() == before == outcome.checkpoint
            assert session.inspect("resume-id") == outcome
            assert await session.resume(message, "resume-id") == outcome
            assert calls == ["resume-id"]
            assert await session.workspace.pending_changes() == []
            assert isinstance(session.inspect("unobserved"), Unknown)
        finally:
            await opened.close()
            client.close()

    asyncio.run(check())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
def test_resume_preserves_unknown_external_outcome(implementation: str, tmp_path: Path) -> None:
    def fail(request: AgentTurnRequest) -> None:
        if request.invocation_id is not None:
            message = "lost provider acceptance"
            raise OSError(message)

    transport, client = _resume_transport(tmp_path, fail)
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    workspace = FakeWorkspace() if implementation == "fake" else _WorkspaceResource()
    message = TemplateRenderer(tmp_path).render_string("trusted result")

    async def check() -> None:
        opened = await _open_resume_contract(implementation, role, workspace, transport)
        session = opened.sessions[0]
        try:
            outcome = await session.resume(message, "ambiguous-id")
            assert isinstance(outcome, Unknown)
            assert session.inspect("ambiguous-id") == outcome
            assert await session.resume(message, "ambiguous-id") == outcome
        finally:
            await opened.close()
            client.close()

    asyncio.run(check())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("cancellations", [1, 2, 4])
def test_cancelled_resume_retains_workspace_until_external_turn_settles(
    implementation: str, cancellations: int, tmp_path: Path
) -> None:
    entered = asyncio.Event()
    release = threading.Event()
    resume_loops: list[asyncio.AbstractEventLoop] = []

    def hold(request: AgentTurnRequest) -> None:
        if request.invocation_id is not None:
            resume_loops[0].call_soon_threadsafe(entered.set)
            release.wait()

    transport, client = _resume_transport(tmp_path, hold)
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    workspace = FakeWorkspace() if implementation == "fake" else _WorkspaceResource()
    message = TemplateRenderer(tmp_path).render_string("trusted result")

    async def check() -> None:
        opened = await _open_resume_contract(implementation, role, workspace, transport)
        session = opened.sessions[0]
        resume_loops.append(asyncio.get_running_loop())
        active = asyncio.create_task(session.resume(message, "held-id"))
        entering = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait((entering, active), return_when=asyncio.FIRST_COMPLETED)
            assert entering in done, "resume ended before reaching the external barrier"
            for _ in range(cancellations):
                active.cancel()
                cancellation_delivered = asyncio.Event()
                asyncio.get_running_loop().call_soon(cancellation_delivered.set)
                await cancellation_delivered.wait()
                assert not active.done()
            close_entered = asyncio.Event()

            async def close_session() -> None:
                close_entered.set()
                await session.close()

            closing = asyncio.create_task(close_session())
            await close_entered.wait()
            assert not closing.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await active
            await closing
            assert session.closed
            assert isinstance(session.inspect("held-id"), Completed)
        finally:
            release.set()
            entering.cancel()
            await asyncio.gather(entering, active, return_exceptions=True)
            await opened.close()
            client.close()

    asyncio.run(check())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("restore_failure", [False, True])
def test_cancelled_resume_drains_workspace_access_enforcement(
    implementation: str, tmp_path: Path, *, restore_failure: bool
) -> None:
    entered = asyncio.Event()
    release = threading.Event()
    resume_loops: list[asyncio.AbstractEventLoop] = []

    class RestoreBlockedFakeWorkspace(FakeWorkspace):
        async def restore_for_agent(
            self, revision: str, *, preserve_paths: tuple[str, ...]
        ) -> None:
            entered.set()
            await asyncio.to_thread(release.wait)
            if restore_failure:
                raise WorkspaceRestoreError(revision)
            await super().restore_for_agent(revision, preserve_paths=preserve_paths)

    class RestoreBlockedRuntimeWorkspace(_WorkspaceResource):
        def restore(
            self,
            revision: str,
            *,
            clean: bool,
            preserve_paths: tuple[str, ...] = (),
            preserve_memory: bool = True,
        ) -> bool:
            resume_loops[0].call_soon_threadsafe(entered.set)
            release.wait()
            return not restore_failure and super().restore(
                revision,
                clean=clean,
                preserve_paths=preserve_paths,
                preserve_memory=preserve_memory,
            )

    workspace = (
        RestoreBlockedFakeWorkspace()
        if implementation == "fake"
        else RestoreBlockedRuntimeWorkspace()
    )

    def mutate(request: AgentTurnRequest) -> None:
        if request.invocation_id is not None:
            if isinstance(workspace, FakeWorkspace):
                workspace.script_pending_changes(["forbidden.py"], [])
            else:
                workspace.changes.append("forbidden.py")

    transport, client = _resume_transport(tmp_path, mutate)
    role = AgentRole(
        id="worker", system_prompt="Work carefully.", workspace_access=WorkspaceAccess.READ_ONLY
    )
    message = TemplateRenderer(tmp_path).render_string("trusted result")

    async def check() -> None:
        opened = await _open_resume_contract(implementation, role, workspace, transport)
        session = opened.sessions[0]
        resume_loops.append(asyncio.get_running_loop())
        active = asyncio.create_task(session.resume(message, "restore-held-id"))
        entering = asyncio.create_task(entered.wait())
        try:
            done, _ = await asyncio.wait((entering, active), return_when=asyncio.FIRST_COMPLETED)
            assert entering in done, "resume ended before reaching the restore barrier"
            for _ in range(2):
                active.cancel()
                cancellation_delivered = asyncio.Event()
                asyncio.get_running_loop().call_soon(cancellation_delivered.set)
                await cancellation_delivered.wait()
                assert not active.done()
            release.set()
            with pytest.raises(asyncio.CancelledError) as cancelled:
                await active
            if restore_failure:
                assert any("WorkspaceRestoreError" in note for note in cancelled.value.__notes__)
            else:
                assert await session.workspace.pending_changes() == []
            assert isinstance(session.inspect("restore-held-id"), Completed)
        finally:
            release.set()
            entering.cancel()
            await asyncio.gather(entering, active, return_exceptions=True)
            await opened.close()
            client.close()

    asyncio.run(check())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize("generation", [1, 2, 17])
def test_member_generation_uses_a_distinct_durable_namespace(
    implementation: str, generation: int, tmp_path: Path
) -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    workspace = FakeWorkspace() if implementation == "fake" else _WorkspaceResource()
    transport, client = _resume_transport(tmp_path, lambda _: None)

    async def check() -> None:
        opened = await _open_resume_contract(implementation, role, workspace, transport)
        try:
            stable = opened.sessions[0]
            generated = await opened.owner.create_session(
                role, workspace=stable.workspace, member_id="member", generation=generation
            )
            assert generated.member_id == stable.member_id == "member"
            assert generated.session_key == AgentSessionKey.for_member(
                role.id, "member", generation=generation
            )
            assert generated.session_key.scope is SessionScope.MEMBER_GENERATION
            assert generated.session_key.durable
            assert generated.session_key != stable.session_key
            assert generated.session_key != AgentSessionKey.for_member(
                role.id, f"member:{generation}"
            )
            assert AgentSessionKey.parse(str(generated.session_key)) == generated.session_key
        finally:
            await opened.close()
            client.close()

    asyncio.run(check())


@pytest.mark.parametrize("implementation", ["fake", "runtime"])
@pytest.mark.parametrize(
    ("member", "generation"), [(None, 1), ("member", 0), ("member", -1), ("member", True)]
)
def test_invalid_session_generation_is_rejected_before_creation(
    implementation: str, member: str | None, generation: int, tmp_path: Path
) -> None:
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    workspace = FakeWorkspace() if implementation == "fake" else _WorkspaceResource()
    transport, client = _resume_transport(tmp_path, lambda _: None)

    async def check() -> None:
        opened = await _open_resume_contract(implementation, role, workspace, transport)
        try:
            with pytest.raises(RuntimeContractError, match="session generation"):
                await opened.owner.create_session(
                    role,
                    workspace=opened.sessions[0].workspace,
                    member_id=member,
                    generation=generation,
                )
            valid = await opened.owner.create_session(
                role, workspace=opened.sessions[0].workspace, member_id="other", generation=1
            )
            assert valid.session_key.durable
        finally:
            await opened.close()
            client.close()

    asyncio.run(check())
