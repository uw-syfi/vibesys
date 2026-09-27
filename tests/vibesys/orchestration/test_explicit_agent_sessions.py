"""Public explicit-session behavior over the production runtime."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import BaseModel, ConfigDict
from tests.vibesys.orchestration.plugin import capability_plugin

import vibesys
from vibesys.api import CoreEvent, OrchestrationRegistry, create_session
from vibesys.composition import AGENT_TOOL_BINDINGS
from vibesys.config import Config
from vibesys.constants import ComputeBackend
from vibesys.inputs import load_input_bundle
from vibesys.orchestration.issue_queue import PLUGIN as ISSUE_QUEUE_PLUGIN
from vibesys.orchestration.profilers import ProfilerKind
from vibesys.orchestration.single import PLUGIN
from vibesys.run.contracts import RunRequest
from vibesys.run.host import open_product_run_host
from vibesys.run.integration import LocalRunIntegration
from vs_agent.api import (
    AgentCapabilities,
    AgentSessionKey,
    SessionScope,
    StdioServerDescriptor,
    ToolServerDescriptor,
)
from vs_agent.api import (
    AgentTurnTimeoutError as DriverAgentTurnTimeoutError,
)
from vs_agent.api.testing import FakeAgentClient, FakeInvocation
from vs_project.api import OrchestrationDescriptor
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentTool,
    AgentTurnTimeoutError,
    CommandResult,
    OrchestrationPlugin,
    Run,
    RunStatus,
    RuntimeContractError,
    SessionClosedError,
    StructuredResponseError,
    UnknownAgentRoleError,
    Workspace,
    WorkspaceAccess,
)
from vs_sandbox.api import HostResource, HostResourceAccess

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping
    from typing import TypeVar

    from vs_runtime.api import AgentSession

    _Result = TypeVar("_Result")


class _Reply(BaseModel):
    value: int


class _Options(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _discard_event(event: CoreEvent) -> None:
    del event


@dataclass(frozen=True, slots=True)
class _RunConfiguration:
    client_factory: Callable[..., _RecordingClient | FakeAgentClient] | None = None
    profiler_kind: ProfilerKind = ProfilerKind.NONE
    domain: str = "generic"
    agent_backend: str = "stub"
    tool_bindings: (
        Mapping[str, Callable[[object, Workspace], tuple[ToolServerDescriptor, ...]]] | None
    ) = None


class _RecordingClient(FakeAgentClient):
    """Give one fake client observable, independently owned cleanup."""

    def __init__(self, name: str, closed: list[str]) -> None:
        super().__init__(session_reuse=True)
        self._name = name
        self._closed_log = closed

    def close(self) -> None:
        """Record cleanup once and close the underlying fake."""
        if not self.closed:
            self._closed_log.append(self._name)
        super().close()


def _write_project(root: Path, *, domain: str = "generic") -> None:
    root.mkdir(parents=True)
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "queue.py").write_text("VALUE = 1\n")
    (root / "memory").mkdir()
    (root / "memory" / "allowed.txt").write_text("before\n")
    (root / "memory" / "seed.txt").write_text("seed\n")
    (root / "vibesys.input.toml").write_text(
        f'version = 1\n[agent]\ndomain = "{domain}"\n'
        '[accuracy]\ncommand = ["true"]\n[benchmark]\ncommand = ["true"]\n'
    )


def _request(
    project_root: Path,
    *,
    orchestration_id: str = "explicit-sessions",
    profiler_kind: ProfilerKind = ProfilerKind.NONE,
    agent_backend: str = "stub",
) -> RunRequest:
    return RunRequest(
        project_root=project_root,
        orchestration=OrchestrationDescriptor(id=orchestration_id, config_version=1, options={}),
        config=Config.model_validate({"model": {"name": "explicit-sessions"}}),
        input_bundle=load_input_bundle(project_root),
        objective="Improve the queue.",
        exp_name="explicit-sessions",
        agent_backend=agent_backend,
        cli_provider="claude",
        profiler_kind=profiler_kind,
        backend=ComputeBackend.CPU,
    )


def _run_with_clients(
    tmp_path: Path,
    clients: list[_RecordingClient | FakeAgentClient],
    body: Callable[[Run], Awaitable[_Result]],
    *,
    declaration: tuple[AgentRole, ...] | OrchestrationPlugin,
    configuration: _RunConfiguration | None = None,
) -> _Result:
    configuration = configuration or _RunConfiguration()
    project_root = tmp_path / "project"
    _write_project(project_root, domain=configuration.domain)
    available = deque(clients)
    integration = LocalRunIntegration()

    def create_client(**_kwargs: object) -> _RecordingClient | FakeAgentClient:
        return available.popleft()

    async def exercise() -> _Result:
        plugin = (
            declaration
            if isinstance(declaration, OrchestrationPlugin)
            else capability_plugin("explicit-sessions", agents=declaration)
        )
        async with open_product_run_host(
            _request(
                project_root,
                orchestration_id=plugin.id,
                profiler_kind=configuration.profiler_kind,
                agent_backend=configuration.agent_backend,
            ),
            integration,
            agent_client_factory=configuration.client_factory or create_client,
            agent_tool_bindings=configuration.tool_bindings,
            plugin=plugin,
        ) as ctx:
            return await body(ctx)

    try:
        return asyncio.run(exercise())
    finally:
        integration.close()


def test_sessions_fix_configuration_and_preserve_distinct_conversations(tmp_path: Path) -> None:
    first = FakeAgentClient(
        capabilities=AgentCapabilities(
            session_reuse=True,
            provider_session_resume=True,
        )
    ).enqueue_text("worker", "one", "two")
    second = FakeAgentClient(session_reuse=True).enqueue_text("worker", "fresh")
    role = AgentRole(id="worker", system_prompt="Work carefully.")

    async def body(ctx: Run) -> None:
        assert ctx.run_id
        one = await ctx.agents.create_session(
            role, workspace=ctx.workspaces.root, member_id="candidate-1"
        )
        two = await ctx.agents.create_session(role, workspace=ctx.workspaces.root)

        assert one.role is role
        assert one.workspace is ctx.workspaces.root
        assert one.member_id == "candidate-1"
        assert await one.turn("first") == "one"
        assert await one.turn("second") == "two"
        assert await two.turn("first") == "fresh"

    _run_with_clients(tmp_path, [first, second], body, declaration=(role,))

    assert first.calls[0].session_key == first.calls[1].session_key
    assert first.calls[0].session_key != second.calls[0].session_key
    assert all(call.reuse_session is True for call in [*first.calls, *second.calls])
    assert all(call.system_prompt == "Work carefully." for call in first.calls)


def test_product_composition_declares_its_runtime_to_confined_agents(tmp_path: Path) -> None:
    client = FakeAgentClient(session_reuse=True)
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    observed: list[HostResource] = []

    def create_client(**kwargs: object) -> FakeAgentClient:
        observed.extend(cast("tuple[HostResource, ...]", kwargs["host_resources"]))
        return client

    async def body(ctx: Run) -> None:
        session = await ctx.agents.create_session(role, workspace=ctx.workspaces.root)
        await session.close()

    _run_with_clients(
        tmp_path,
        [client],
        body,
        declaration=(role,),
        configuration=_RunConfiguration(client_factory=create_client),
    )

    package_root = Path(vibesys.__file__).resolve().parents[1]
    matching = [resource for resource in observed if resource.path == package_root]
    assert matching == [HostResource(package_root, HostResourceAccess.READ_ONLY, "VibeSys runtime")]


def test_named_session_identity_is_durable_and_binding_is_visible(tmp_path: Path) -> None:
    member_id = "H-01 / Trial #3"
    capabilities = AgentCapabilities(
        session_reuse=True,
        provider_session_resume=True,
    )
    first = FakeAgentClient(
        backend_name="cli",
        driver_name="agentshim",
        provider="codex",
        model="gpt-6-sol",
        capabilities=capabilities,
    ).enqueue_text("worker", "one")
    second = FakeAgentClient(capabilities=capabilities).enqueue_text("worker", "two")
    role = AgentRole(id="worker", system_prompt="Work carefully.")

    async def body(ctx: Run) -> None:
        one = await ctx.agents.create_session(
            role,
            workspace=ctx.workspaces.root,
            member_id=member_id,
        )
        assert one.binding.backend == "cli"
        assert one.binding.driver == "agentshim"
        assert one.binding.provider == "codex"
        assert one.binding.model == "gpt-6-sol"
        assert await one.turn("start") == "one"
        await one.close()

        continued = await ctx.agents.create_session(
            role,
            workspace=ctx.workspaces.root,
            member_id=member_id,
        )
        assert await continued.turn("continue") == "two"

    _run_with_clients(tmp_path, [first, second], body, declaration=(role,))

    expected = AgentSessionKey(
        SessionScope.MEMBER,
        f"worker:{member_id}",
    )
    assert expected.durable
    assert first.calls[0].session_key == expected
    assert second.calls[0].session_key == expected


def test_named_session_requires_cross_process_resume_support(tmp_path: Path) -> None:
    client = FakeAgentClient(session_reuse=True)
    role = AgentRole(id="worker", system_prompt="Work carefully.")

    async def body(ctx: Run) -> None:
        with pytest.raises(RuntimeContractError, match="provider_session_resume"):
            await ctx.agents.create_session(
                role,
                workspace=ctx.workspaces.root,
                member_id="candidate-1",
            )

    _run_with_clients(tmp_path, [client], body, declaration=(role,))
    assert client.closed


def test_typed_parse_failure_is_not_replaced_by_a_fallback(tmp_path: Path) -> None:
    client = FakeAgentClient(session_reuse=True)
    client.enqueue("judge", _Reply(value=3)).enqueue_parse_failure("judge")
    role = AgentRole(id="judge", system_prompt="Return JSON.")

    async def body(ctx: Run) -> None:
        session = await ctx.agents.create_session(role, workspace=ctx.workspaces.root)
        assert await session.turn("valid", response=_Reply) == _Reply(value=3)
        with pytest.raises(StructuredResponseError, match="valid _Reply response"):
            await session.turn("invalid", response=_Reply)

    _run_with_clients(tmp_path, [client], body, declaration=(role,))


def test_plugin_policy_receives_only_the_runtime_timeout_contract(tmp_path: Path) -> None:
    driver_error = DriverAgentTurnTimeoutError(12.5)
    client = FakeAgentClient(session_reuse=True).fail("worker", driver_error, times=1)
    role = AgentRole(id="worker", system_prompt="Work carefully.")
    observed: list[AgentTurnTimeoutError] = []

    async def orchestrate(ctx: Run, _options: BaseModel) -> RunStatus:
        session = await ctx.agents.create_session(role, workspace=ctx.workspaces.root)
        try:
            await session.turn("work")
        except AgentTurnTimeoutError as error:
            observed.append(error)
            return RunStatus.SUCCEEDED
        return RunStatus.FAILED

    plugin = OrchestrationPlugin(
        id="timeout-policy",
        agents=(role,),
        options=_Options,
        orchestrate=orchestrate,
    )

    async def body(ctx: Run) -> RunStatus:
        return await plugin.orchestrate(ctx, _Options())

    status = _run_with_clients(tmp_path, [client], body, declaration=plugin)

    assert status is RunStatus.SUCCEEDED
    assert len(observed) == 1
    assert observed[0].timeout_seconds == 12.5
    assert observed[0].__cause__ is driver_error


def test_session_rejects_undeclared_role_and_missing_driver_capability(tmp_path: Path) -> None:
    declared = AgentRole(id="worker", system_prompt="Declared.")
    altered = AgentRole(id="worker", system_prompt="Not declared.")
    requires_resume = AgentRole(
        id="resumer",
        system_prompt="Resume.",
        required_capabilities=frozenset({AgentCapability.PROVIDER_SESSION_RESUME}),
    )
    unsupported_tool = AgentRole(
        id="tool-user",
        system_prompt="Use a tool.",
        tools=(AgentTool(id="unregistered"),),
    )
    unsupported_skill = AgentRole(
        id="skill-user",
        system_prompt="Use a skill.",
        skills=("profiling",),
    )
    client = FakeAgentClient(session_reuse=True)

    async def body(ctx: Run) -> None:
        with pytest.raises(UnknownAgentRoleError, match="worker"):
            await ctx.agents.create_session(altered, workspace=ctx.workspaces.root)
        with pytest.raises(RuntimeContractError, match="unsupported agent tools"):
            await ctx.agents.create_session(unsupported_tool, workspace=ctx.workspaces.root)
        with pytest.raises(RuntimeContractError, match="role-scoped agent skills"):
            await ctx.agents.create_session(unsupported_skill, workspace=ctx.workspaces.root)
        with pytest.raises(RuntimeContractError, match="provider_session_resume"):
            await ctx.agents.create_session(requires_resume, workspace=ctx.workspaces.root)

    _run_with_clients(
        tmp_path,
        [client],
        body,
        declaration=(declared, requires_resume, unsupported_tool, unsupported_skill),
    )
    assert client.closed


def test_session_rejects_unknown_tool_before_spawning(tmp_path: Path) -> None:
    role = AgentRole(
        id="tool-user",
        system_prompt="Use the declared tool.",
        tools=(AgentTool(id="unknown"),),
    )

    async def body(ctx: Run) -> None:
        with pytest.raises(RuntimeContractError, match="unsupported agent tools: unknown"):
            await ctx.agents.create_session(role, workspace=ctx.workspaces.root)

    _run_with_clients(tmp_path, [], body, declaration=(role,))


def test_direct_host_has_no_implicit_profiler_tool_binding(tmp_path: Path) -> None:
    role = AgentRole(
        id="profiler",
        system_prompt="Analyze the profile.",
        tools=(AgentTool(id="profiler"),),
    )

    async def body(ctx: Run) -> None:
        with pytest.raises(RuntimeContractError, match="unsupported agent tools: profiler"):
            await ctx.agents.create_session(role, workspace=ctx.workspaces.root)

    _run_with_clients(tmp_path, [], body, declaration=(role,))


def test_public_session_supplies_product_profiler_tool_binding(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    _write_project(project_root)
    role = AgentRole(
        id="profiler",
        system_prompt="Analyze the profile.",
        tools=(AgentTool(id="profiler"),),
    )
    observed: list[str] = []

    async def orchestrate(run: Run, _options: BaseModel) -> RunStatus:
        agent_session = await run.agents.create_session(role, workspace=run.workspaces.root)
        observed.append(agent_session.binding.backend)
        await agent_session.close()
        return RunStatus.SUCCEEDED

    plugin = OrchestrationPlugin(
        id="composed-profiler",
        agents=(role,),
        options=_Options,
        orchestrate=orchestrate,
    )
    registry = OrchestrationRegistry()
    registry.register_plugin(plugin)
    session = create_session(
        _request(project_root, orchestration_id=plugin.id),
        sink=_discard_event,
        registry=registry,
    )

    result = asyncio.run(session.await_result())

    assert result.succeeded
    assert observed == ["stub"]


def test_session_closes_agent_when_bound_tool_requires_unsupported_mcp(
    tmp_path: Path,
) -> None:
    client = FakeAgentClient(session_reuse=True)
    role = AgentRole(
        id="profiler",
        system_prompt="Analyze the profile.",
        tools=(AgentTool(id="profiler"),),
    )

    async def body(ctx: Run) -> None:
        with pytest.raises(RuntimeContractError, match="tool_servers"):
            await ctx.agents.create_session(role, workspace=ctx.workspaces.root)

    _run_with_clients(
        tmp_path,
        [client],
        body,
        declaration=(role,),
        configuration=_RunConfiguration(tool_bindings=AGENT_TOOL_BINDINGS),
    )
    assert client.closed
    assert not client.calls


def test_session_resolves_bound_tools_once_and_reuses_specs_for_every_turn(
    tmp_path: Path,
) -> None:
    client = FakeAgentClient(capabilities=AgentCapabilities(session_reuse=True, tool_servers=True))
    client.enqueue_text("profiler", "analysis").enqueue("profiler", _Reply(value=4))
    resolutions = 0

    def bind_profiler(run: object, workspace: Workspace) -> tuple[ToolServerDescriptor, ...]:
        nonlocal resolutions
        resolutions += 1
        return AGENT_TOOL_BINDINGS["profiler"](run, workspace)

    role = AgentRole(
        id="profiler",
        system_prompt="Analyze the profile.",
        tools=(AgentTool(id="profiler"),),
    )

    async def body(ctx: Run) -> None:
        session = await ctx.agents.create_session(role, workspace=ctx.workspaces.root)
        assert await session.turn("inspect") == "analysis"
        assert await session.turn("summarize", response=_Reply) == _Reply(value=4)

    _run_with_clients(
        tmp_path,
        [client],
        body,
        declaration=(role,),
        configuration=_RunConfiguration(
            profiler_kind=ProfilerKind.OTEL,
            domain="microservices",
            agent_backend="cli",
            tool_bindings={"profiler": bind_profiler},
        ),
    )
    assert resolutions == 1
    assert client.calls[0].tool_servers == client.calls[1].tool_servers
    assert client.calls[0].tool_servers == [
        StdioServerDescriptor(
            name="vibesys-otel-profiler",
            command="python",
            args=("otel_profiler/server.py",),
        )
    ]


def test_tool_resolver_receives_selected_workspace_once(tmp_path: Path) -> None:
    client = FakeAgentClient(capabilities=AgentCapabilities(session_reuse=True, tool_servers=True))
    role = AgentRole(
        id="worker",
        system_prompt="Use the workspace-scoped tool.",
        tools=(AgentTool(id="workspace-tool"),),
    )
    resolved_workspaces: list[Workspace] = []

    def bind_tool(_host: object, workspace: Workspace) -> tuple[ToolServerDescriptor, ...]:
        resolved_workspaces.append(workspace)
        return (StdioServerDescriptor(name="workspace-tool", command="tool-server"),)

    async def body(ctx: Run) -> None:
        session = await ctx.agents.create_session(role, workspace=ctx.workspaces.root)
        assert resolved_workspaces == [ctx.workspaces.root]
        await session.close()

    _run_with_clients(
        tmp_path,
        [client],
        body,
        declaration=(role,),
        configuration=_RunConfiguration(tool_bindings={"workspace-tool": bind_tool}),
    )
    assert resolved_workspaces
    assert client.closed


def test_issue_board_binding_uses_fixed_workspace_relative_spec(tmp_path: Path) -> None:
    client = FakeAgentClient(
        capabilities=AgentCapabilities(session_reuse=True, tool_servers=True)
    ).enqueue_text("judge", "reviewed")
    role = AgentRole(
        id="judge",
        system_prompt="Review the issue.",
        tools=(AgentTool(id="issue-board"),),
    )

    async def body(ctx: Run) -> None:
        session = await ctx.agents.create_session(role, workspace=ctx.workspaces.root)
        assert await session.turn("review") == "reviewed"

    _run_with_clients(
        tmp_path,
        [client],
        body,
        declaration=(role,),
        configuration=_RunConfiguration(tool_bindings=AGENT_TOOL_BINDINGS),
    )
    assert client.calls[0].tool_servers == [
        StdioServerDescriptor(
            name="vibesys-issue-board",
            command="python",
            args=(
                "-m",
                "vibesys.orchestration.issue_queue.tool_server",
                "issues.json",
                ".vibesys/issue-tool-policy.json",
                ".vibesys/issue-tracker.json",
            ),
        )
    ]


def test_issue_queue_plugin_can_create_all_declared_sessions(tmp_path: Path) -> None:
    capabilities = AgentCapabilities(
        session_reuse=True,
        provider_session_resume=True,
        tool_servers=True,
    )
    clients = [FakeAgentClient(capabilities=capabilities) for _role in ISSUE_QUEUE_PLUGIN.agents]

    async def body(ctx: Run) -> None:
        sessions = [
            await ctx.agents.create_session(
                role,
                workspace=ctx.workspaces.root,
                member_id=f"issue-queue-{role.id}",
            )
            for role in ISSUE_QUEUE_PLUGIN.agents
        ]
        assert [session.role for session in sessions] == list(ISSUE_QUEUE_PLUGIN.agents)

    _run_with_clients(
        tmp_path,
        clients,
        body,
        declaration=ISSUE_QUEUE_PLUGIN,
        configuration=_RunConfiguration(tool_bindings=AGENT_TOOL_BINDINGS),
    )
    assert all(client.closed for client in clients)


def test_intrinsic_shell_tool_does_not_bind_an_mcp_server(tmp_path: Path) -> None:
    client = FakeAgentClient(session_reuse=True).enqueue_text("worker", "done")
    role = AgentRole(
        id="worker",
        system_prompt="Work in the shell.",
        tools=(AgentTool(id="shell"),),
    )

    async def body(ctx: Run) -> None:
        session = await ctx.agents.create_session(role, workspace=ctx.workspaces.root)
        assert await session.turn("work") == "done"

    _run_with_clients(tmp_path, [client], body, declaration=(role,))
    assert client.calls[0].tool_servers is None


def test_read_only_session_restores_writes_and_early_close_is_idempotent(
    tmp_path: Path,
) -> None:
    client = FakeAgentClient(session_reuse=True)

    def write_workspace(invocation: FakeInvocation) -> str:
        workspace = invocation.workspace
        (workspace / "queue.py").write_text("VALUE = 2\n")
        return "done"

    client.enqueue_text("reviewer", write_workspace)
    role = AgentRole(
        id="reviewer",
        system_prompt="Review only.",
        workspace_access=WorkspaceAccess.READ_ONLY,
    )

    async def body(ctx: Run) -> None:
        session = await ctx.agents.create_session(role, workspace=ctx.workspaces.root)
        assert await session.turn("review") == "done"
        assert (ctx.workspaces.root.path / "queue.py").read_text() == "VALUE = 1\n"
        await session.close()
        await session.close()
        assert session.closed
        with pytest.raises(SessionClosedError):
            await session.turn("too late")

    _run_with_clients(tmp_path, [client], body, declaration=(role,))
    assert client.closed


def test_limited_session_preserves_granted_directory_and_reverts_other_writes(
    tmp_path: Path,
) -> None:
    client = FakeAgentClient(session_reuse=True)

    def write_workspace(invocation: FakeInvocation) -> str:
        (invocation.workspace / "memory" / "notes.md").write_text("kept\n")
        (invocation.workspace / "queue.py").write_text("VALUE = 2\n")
        return "done"

    client.enqueue_text("designer", write_workspace)
    role = AgentRole(
        id="designer",
        system_prompt="Write only in memory/.",
        workspace_access=WorkspaceAccess.LIMITED,
    )

    async def body(ctx: Run) -> None:
        session = await ctx.agents.create_session(
            role,
            workspace=ctx.workspaces.root,
            writable_paths=("memory",),
        )
        assert session.writable_paths == ("memory",)
        attribute = "writable_paths"
        with pytest.raises(AttributeError):
            setattr(session, attribute, ("queue.py",))
        assert await session.turn("write") == "done"
        assert (ctx.workspaces.root.path / "memory" / "notes.md").read_text() == "kept\n"
        assert (ctx.workspaces.root.path / "queue.py").read_text() == "VALUE = 1\n"

    _run_with_clients(tmp_path, [client], body, declaration=(role,))


def test_limited_file_grant_does_not_authorize_sibling_files(tmp_path: Path) -> None:
    client = FakeAgentClient(session_reuse=True)

    def write_workspace(invocation: FakeInvocation) -> str:
        (invocation.workspace / "memory" / "allowed.txt").write_text("after\n")
        (invocation.workspace / "memory" / "sibling.txt").write_text("revert\n")
        return "done"

    client.enqueue_text("profiler", write_workspace)
    role = AgentRole(
        id="profiler",
        system_prompt="Write only the granted file.",
        workspace_access=WorkspaceAccess.LIMITED,
    )

    async def body(ctx: Run) -> None:
        session = await ctx.agents.create_session(
            role,
            workspace=ctx.workspaces.root,
            writable_paths=("memory/allowed.txt",),
        )
        assert await session.turn("write") == "done"
        assert (ctx.workspaces.root.path / "memory" / "allowed.txt").read_text() == "after\n"
        assert not (ctx.workspaces.root.path / "memory" / "sibling.txt").exists()

    _run_with_clients(tmp_path, [client], body, declaration=(role,))


def test_run_cleanup_closes_sessions_in_reverse_creation_order(tmp_path: Path) -> None:
    closed: list[str] = []
    first = _RecordingClient("first", closed)
    second = _RecordingClient("second", closed)
    role = AgentRole(id="worker", system_prompt="Work.")
    sessions: list[AgentSession] = []

    async def body(ctx: Run) -> None:
        sessions.append(await ctx.agents.create_session(role, workspace=ctx.workspaces.root))
        sessions.append(await ctx.agents.create_session(role, workspace=ctx.workspaces.root))

    _run_with_clients(tmp_path, [first, second], body, declaration=(role,))
    assert closed == ["second", "first"]
    assert all(session.closed for session in sessions)


def test_canceled_session_construction_closes_the_opened_agent(tmp_path: Path) -> None:
    client = FakeAgentClient(session_reuse=True)
    factory_started = threading.Event()
    factory_release = threading.Event()
    role = AgentRole(id="worker", system_prompt="Work.")

    def create_client(**_kwargs: object) -> FakeAgentClient:
        factory_started.set()
        factory_release.wait()
        return client

    async def body(ctx: Run) -> None:
        creation = asyncio.create_task(
            ctx.agents.create_session(role, workspace=ctx.workspaces.root)
        )
        await asyncio.to_thread(factory_started.wait)
        creation.cancel()
        factory_release.set()
        with pytest.raises(asyncio.CancelledError):
            await creation
        assert client.closed

    _run_with_clients(
        tmp_path,
        [],
        body,
        declaration=(role,),
        configuration=_RunConfiguration(client_factory=create_client),
    )


def test_plugin_orchestrates_through_the_live_host(tmp_path: Path) -> None:
    designer = FakeAgentClient(session_reuse=True).enqueue(
        "orchestrator",
        {
            "hypothesis_id": "batch-prefill",
            "hypothesis": "batching removes launch overhead",
            "task": "batch prefill",
            "pass_criteria": "throughput improves without an accuracy regression",
            "reasoning": "launch overhead dominates",
        },
    )
    implementer = FakeAgentClient(
        capabilities=AgentCapabilities(
            session_reuse=True,
            provider_session_resume=True,
        )
    ).enqueue(
        "implementer",
        {
            "summary": "implemented batching",
            "expected_behavior": "higher throughput",
            "self_review": "reviewed the diff and ran checks",
            "feedback": "",
            "verdict": "pass",
            "bottlenecks": "launch overhead",
            "suggestions": "batch prefill",
            "profile_analysis": "batching should reduce launches",
        },
    )

    async def body(ctx: Run) -> None:
        status = await PLUGIN.orchestrate(
            ctx,
            PLUGIN.options.model_validate(
                {
                    "interface": "inprocess",
                    "max_rounds": 1,
                    "max_retries_per_round": 1,
                    "judge_every": 1,
                    "official_eval_every": 1,
                }
            ),
        )
        assert status.value == "succeeded"

    _run_with_clients(
        tmp_path,
        [designer, implementer],
        body,
        declaration=PLUGIN,
    )
    assert [call.kind for call in designer.calls] == ["orchestrator"]
    assert [call.kind for call in implementer.calls] == ["implementer"]


def test_plugin_command_execution_quotes_argv_and_scopes_to_workspace(tmp_path: Path) -> None:
    role = AgentRole(id="unused", system_prompt="Unused.")

    async def body(ctx: Run) -> None:
        result = await ctx.commands.run(
            (
                "python",
                "-c",
                "import sys; print(sys.argv[1])",
                "literal; printf not-a-second-command",
            ),
            workspace=ctx.workspaces.root,
            timeout_seconds=10,
        )

        assert result.exit_code == 0
        assert result.output.strip() == "literal; printf not-a-second-command"
        assert not result.truncated

    _run_with_clients(tmp_path, [], body, declaration=(role,))


def test_plugin_command_capture_owns_bounded_output_file_lifecycle(tmp_path: Path) -> None:
    role = AgentRole(id="unused", system_prompt="Unused.")

    async def body(ctx: Run) -> None:
        result = await ctx.commands.capture_output(
            (
                "python",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "path = Path(sys.argv[-1]); path.write_text(str(path))"
                ),
            ),
            workspace=ctx.workspaces.root,
            output_argument="--result-file",
            timeout_seconds=10,
        )

        assert result.exit_code == 0
        assert result.output
        cleanup = await ctx.commands.run(
            (
                "python",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "raise SystemExit(Path(sys.argv[1]).exists())"
                ),
                result.output,
            ),
            workspace=ctx.workspaces.root,
        )
        assert cleanup.exit_code == 0

        failed = await ctx.commands.capture_output(
            (
                "python",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "path = Path(sys.argv[-1]); path.write_text('unused'); "
                    "print(path); raise SystemExit(7)"
                ),
            ),
            workspace=ctx.workspaces.root,
            output_argument="--result-file",
        )
        assert failed.exit_code == 7
        failed_cleanup = await ctx.commands.run(
            (
                "python",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "raise SystemExit(Path(sys.argv[1]).exists())"
                ),
                failed.output.splitlines()[0],
            ),
            workspace=ctx.workspaces.root,
        )
        assert failed_cleanup.exit_code == 0

        empty = await ctx.commands.capture_output(
            (
                "python",
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[-1]).write_text('')",
            ),
            workspace=ctx.workspaces.root,
            output_argument="--result-file",
        )
        assert empty == CommandResult(output="", exit_code=0)

        large = await ctx.commands.capture_output(
            (
                "python",
                "-c",
                (
                    "from pathlib import Path; import sys; "
                    "Path(sys.argv[-1]).write_text('x' * 150000)"
                ),
            ),
            workspace=ctx.workspaces.root,
            output_argument="--result-file",
        )
        assert large.truncated
        assert len(large.output) < 150_000

    _run_with_clients(tmp_path, [], body, declaration=(role,))


def test_plugin_command_capture_reports_cleanup_failure(tmp_path: Path) -> None:
    role = AgentRole(id="unused", system_prompt="Unused.")

    async def body(ctx: Run) -> None:
        with pytest.raises(
            RuntimeContractError,
            match="without a readable captured output file",
        ) as captured:
            await ctx.commands.capture_output(
                (
                    "python",
                    "-c",
                    (
                        "from pathlib import Path; import sys; "
                        "path = Path(sys.argv[-1]); "
                        "Path('capture-path.txt').write_text(str(path)); "
                        "path.unlink(); path.mkdir()"
                    ),
                ),
                workspace=ctx.workspaces.root,
                output_argument="--result-file",
            )
        assert any(
            "could not remove a captured command output file" in note
            for note in captured.value.__notes__
        )

    try:
        _run_with_clients(tmp_path, [], body, declaration=(role,))
    finally:
        recorded = tmp_path / "project" / "capture-path.txt"
        if recorded.exists():
            Path(recorded.read_text()).rmdir()
