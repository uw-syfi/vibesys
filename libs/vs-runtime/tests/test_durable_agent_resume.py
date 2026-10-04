"""Same-client continuation and reconstruction through the workspace API."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, replace
from io import StringIO
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import BaseModel
from tests.support.run_execution import run_execution_record

from vs_agent.api import (
    NULL_AGENT_EVENT_SINK,
    AgentCapabilities,
    AgentClient,
    AgentInvocationState,
    AgentSessionState,
    AgentSpec,
    AgentTurnRequest,
    Completed,
    DurableSessionStore,
    InvocationConflictError,
    Pending,
    StdioServerDescriptor,
)
from vs_agent.api.testing import FakeDriver
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentTool,
    RuntimeContractError,
    WorkspaceAccess,
)
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionScope,
    AgentExecutionStarted,
    BlockingOperations,
    WorkspaceEvaluationSpec,
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
from vs_sandbox.api import (
    HostResource,
    HostResourceAccess,
    ProjectPathPolicy,
    SandboxExecutionResult,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import (
        TrustedAccuracyResult,
        TrustedBenchmarkResult,
        WorkspaceRuntime,
    )


class Reply(BaseModel):
    value: int


class WorkspaceResource:
    """Immutable root resource with durable snapshots and no candidate work."""

    id = None
    revision = "a" * 40
    trusted_input_baseline = "a" * 40
    evaluation_spec = WorkspaceEvaluationSpec(None, None, None, None)

    def __init__(self, path: Path) -> None:
        self.path = path
        self.closed = False

    def snapshot(self, label: str) -> str:
        del label
        return self.revision

    def pending_changes(self) -> list[str]:
        return []

    def is_directory(self, path: str) -> bool:
        return (self.path / path).is_dir()

    def restore(
        self,
        revision: str,
        *,
        clean: bool,
        preserve_paths: tuple[str, ...] = (),
        preserve_memory: bool = True,
    ) -> bool:
        del clean, preserve_paths, preserve_memory
        return revision == self.revision

    def try_restore(self, revision: str, *, clean: bool) -> bool:
        return self.restore(revision, clean=clean)

    def retain(self, revision: str, reference: str) -> None:
        del revision, reference

    def candidate_patch(self, revision: str) -> str:
        del revision
        return ""

    def trusted_input_changes(self) -> list[str]:
        return []

    def execute(self, command: str, timeout_seconds: int | None) -> SandboxExecutionResult:
        del command, timeout_seconds
        return SandboxExecutionResult("", 0)

    def agent_scope(self) -> AgentExecutionScope:
        def environment(_: AgentExecutionConfiguration) -> FakeAgentExecutionEnvironment:
            return FakeAgentExecutionEnvironment(
                project_path_policy=ProjectPathPolicy(),
                host_resources=(
                    HostResource(self.path, HostResourceAccess.READ_ONLY, "environment grant"),
                ),
            )

        return AgentExecutionScope(
            workspace_path=self.path,
            log_directory=self.path,
            open_environment=environment,
            current_log_file=StringIO,
            environment_variables=dict,
        )

    async def trusted_accuracy(self, command_override: str | None) -> TrustedAccuracyResult:
        del command_override
        raise NotImplementedError

    async def trusted_benchmark(
        self, command_override: str | None, required_metrics: frozenset[str]
    ) -> TrustedBenchmarkResult:
        del command_override, required_metrics
        raise NotImplementedError

    def close(self) -> None:
        self.closed = True


@dataclass(frozen=True)
class Resources:
    root: WorkspaceResource
    supports_parallel_candidates: bool = False

    def create_candidate(self, workspace_id: str, revision: str) -> WorkspaceResource:
        del workspace_id, revision
        raise NotImplementedError


ROLE = AgentRole(
    id="worker",
    system_prompt="Work carefully.",
    workspace_access=WorkspaceAccess.READ_ONLY,
    extra_tools=(AgentTool(id="diagnostic"),),
    required_capabilities=frozenset({AgentCapability.DURABLE_TURN_CONTINUATION}),
)


def open_runtime(
    project: Project,
    path: Path,
    driver: FakeDriver,
    lifecycle: FakeAgentExecutionLifecycleSink | None = None,
) -> WorkspaceRuntime:
    namespace = project.state.local_namespace("run-1", "agent")

    def client(**kwargs: object) -> AgentClient:
        spec = cast("AgentSpec", kwargs["spec"])
        return AgentClient(
            driver,
            provider="fake",
            model_name=spec.model,
            role_models=spec.role_models,
            default_reasoning_effort=spec.reasoning_effort,
            role_reasoning_efforts=spec.role_reasoning_efforts,
            timeout=spec.cli_timeout,
            project_path_policy=cast("ProjectPathPolicy", kwargs["project_path_policy"]),
            host_resources=cast("tuple[HostResource, ...]", kwargs["host_resources"]),
            require_host_sandbox=cast("bool", kwargs["require_host_sandbox"]),
            skills=cast("list[Path]", kwargs["skill_source_dirs"]),
            containerized=cast("bool", kwargs["use_docker"]),
            session_store=DurableSessionStore(namespace.slot("sessions.json", AgentSessionState)),
        )

    return create_workspace_runtime(
        (ROLE,),
        workspace_resources=Resources(WorkspaceResource(path)),
        resolve_configuration=lambda _: AgentExecutionConfiguration(
            "worker",
            AgentSpec(
                model="default-model",
                role_models={"worker": "worker-model"},
                reasoning_effort="medium",
                role_reasoning_efforts={"worker": "high"},
                cli_timeout=90,
            ),
            resources=(
                HostResource(path / "extra", HostResourceAccess.READ_ONLY, "configuration grant"),
            ),
        ),
        session_store=lambda: None,
        invocation_store=lambda _: namespace.slot("invocations.json", AgentInvocationState),
        control=create_run_control_channel(FakeRunControlEventSink()),
        lifecycle_events=lifecycle or FakeAgentExecutionLifecycleSink(),
        agent_events=NULL_AGENT_EVENT_SINK,
        route_message=lambda message, _: message,
        blocking=BlockingOperations(),
        client_factory=client,
        tool_bindings={
            "diagnostic": lambda _: (
                StdioServerDescriptor(
                    "diagnostic",
                    "diagnostic-command",
                    args=("--flag",),
                    env=(("TOKEN", "fake-token"),),
                ),
            )
        },
    )


def create_project(tmp_path: Path) -> Project:
    project = Project.open(tmp_path)
    project.state.create_project("continuations")
    manifest = project.state.new_run_manifest(
        "continuations",
        run_id="run-1",
        trusted_input_baseline="a" * 40,
        branch="test/continuations",
        vibesys_version="test",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="test", config_version=1, options={}),
    )
    project.state.create_run(manifest)
    return project


def test_initial_turn_and_reconstructed_resume_use_the_same_conversation(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    lifecycle = FakeAgentExecutionLifecycleSink()
    turns: list[AgentTurnRequest] = []
    initial_driver = FakeDriver(answer={"value": 7}, on_turn=turns.append)
    reconstructed_driver = FakeDriver(answer={"value": 8}, on_turn=turns.append)
    # A local template keeps the continuation at the checked agent-bound sink.
    template = tmp_path / "resume.j2"
    template.write_text("{{ result }}")
    message = TemplateRenderer(tmp_path).render_template("resume.j2", result="trusted result")

    async def scenario() -> None:
        runtime = open_runtime(project, tmp_path, initial_driver, lifecycle)
        session = await runtime.agents.create_session(
            ROLE, workspace=runtime.workspaces.root, member_id="member"
        )
        assert await session.turn("initial", response=Reply) == Reply(value=7)
        checkpoint = session.checkpoint()
        first = await session.resume(message, "resume-1", response=Reply)
        assert isinstance(first, Completed)
        assert first.checkpoint == checkpoint
        assert len(turns) == 2
        assert await session.resume(message, "resume-1", response=Reply) == first
        assert len(turns) == 2
        assert (
            len(
                [
                    event
                    for event in lifecycle.events
                    if isinstance(event, AgentExecutionStarted) and event.execution_id == "resume-1"
                ]
            )
            == 1
        )
        changed = TemplateRenderer(tmp_path).render_template("resume.j2", result="changed result")
        with pytest.raises(InvocationConflictError):
            await session.resume(changed, "resume-1", response=Reply)
        assert len(turns) == 2
        await runtime.workspaces.close()

        reopened = open_runtime(project, tmp_path, reconstructed_driver)
        session = await reopened.agents.create_session(
            ROLE, workspace=reopened.workspaces.root, member_id="member"
        )
        assert session.inspect("resume-1") == first
        second = await session.resume(message, "resume-2", response=Reply)
        assert isinstance(second, Completed)
        assert second.checkpoint == checkpoint
        assert Reply.model_validate_json(second.result.text) == Reply(value=8)
        assert len(turns) == 3
        assert reconstructed_driver.resumed_session_ids == (checkpoint.provider_session_id,)
        await reopened.workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["runtime", "fake"])
def test_durable_key_has_one_live_owner(implementation: str, tmp_path: Path) -> None:
    async def scenario() -> None:
        runtime = None
        if implementation == "runtime":
            runtime = open_runtime(
                create_project(tmp_path), tmp_path, FakeDriver(answer={"value": 7})
            )
            owner = runtime.agents
            workspace = runtime.workspaces.root
        else:
            owner = FakeWorkspaceAgentSessions(
                (ROLE,),
                supported_extra_tools={"diagnostic"},
                supported_agent_capabilities={
                    AgentCapability.MCP_SERVERS,
                    AgentCapability.DURABLE_TURN_CONTINUATION,
                    AgentCapability.PROVIDER_SESSION_RESUME,
                },
            )
            workspace = FakeWorkspace()
        first = await owner.create_session(ROLE, workspace=workspace, member_id="member")
        with pytest.raises(RuntimeContractError, match="already has a live owner"):
            await owner.create_session(ROLE, workspace=workspace, member_id="member")
        await first.close()
        reopened = await owner.create_session(ROLE, workspace=workspace, member_id="member")
        assert reopened.session_key == first.session_key
        await owner.close()
        if runtime is not None:
            await runtime.workspaces.close()

    asyncio.run(scenario())


def test_in_flight_resume_is_inspectable_without_waiting_for_the_agent(tmp_path: Path) -> None:
    project = create_project(tmp_path)
    entered = threading.Event()
    released = threading.Event()

    def hold(request: AgentTurnRequest) -> None:
        if request.label == "evaluation-resume":
            entered.set()
            released.wait()

    message = TemplateRenderer(tmp_path).render_string("trusted result")

    async def scenario() -> None:
        runtime = open_runtime(project, tmp_path, FakeDriver(answer={"value": 7}, on_turn=hold))
        session = await runtime.agents.create_session(
            ROLE, workspace=runtime.workspaces.root, member_id="member"
        )
        await session.turn("initial", response=Reply)
        resume = asyncio.create_task(session.resume(message, "resume-1", response=Reply))
        await asyncio.to_thread(entered.wait)
        try:
            observed = session.inspect("resume-1")
            assert isinstance(observed, Pending)
            assert not resume.done()
        finally:
            released.set()
        assert isinstance(await resume, Completed)
        await runtime.workspaces.close()

    asyncio.run(scenario())


class NonresumableDriver(FakeDriver):
    @property
    def capabilities(self) -> AgentCapabilities:
        return replace(super().capabilities, provider_session_resume=False)


def test_durable_continuation_rejects_a_driver_without_resume_before_a_turn(tmp_path: Path) -> None:
    calls: list[AgentTurnRequest] = []
    driver = NonresumableDriver(answer={"value": 7}, on_turn=calls.append)

    async def scenario() -> None:
        runtime = open_runtime(create_project(tmp_path), tmp_path, driver)
        with pytest.raises(RuntimeContractError, match="durable_turn_continuation"):
            await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
        assert not calls
        await runtime.workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["runtime", "fake"])
def test_key_ownership_is_held_until_pending_close_acknowledges(
    implementation: str, tmp_path: Path
) -> None:
    worker_entered = threading.Event()
    worker_released = threading.Event()
    fake_entered = asyncio.Event()
    fake_released = asyncio.Event()

    def hold(_request: AgentTurnRequest) -> None:
        worker_entered.set()
        worker_released.wait()

    async def respond(
        _role: AgentRole,
        _history: tuple[str, ...],
        _message: str,
        _response: type[BaseModel] | None,
    ) -> str:
        fake_entered.set()
        await fake_released.wait()
        return "done"

    async def scenario() -> None:
        runtime = None
        if implementation == "runtime":
            runtime = open_runtime(
                create_project(tmp_path), tmp_path, FakeDriver(answer="done", on_turn=hold)
            )
            owner = runtime.agents
            workspace = runtime.workspaces.root
        else:
            owner = FakeWorkspaceAgentSessions(
                (ROLE,),
                responder=respond,
                supported_extra_tools={"diagnostic"},
                supported_agent_capabilities={
                    AgentCapability.MCP_SERVERS,
                    AgentCapability.PROVIDER_SESSION_RESUME,
                    AgentCapability.DURABLE_TURN_CONTINUATION,
                },
            )
            workspace = FakeWorkspace()
        session = await owner.create_session(ROLE, workspace=workspace, member_id="member")
        turn = asyncio.create_task(session.turn("initial"))
        if implementation == "runtime":
            await asyncio.to_thread(worker_entered.wait)
        else:
            await fake_entered.wait()
        close_started = asyncio.Event()

        async def close() -> None:
            close_started.set()
            await session.close()

        closing = asyncio.create_task(close())
        await close_started.wait()
        assert session.closed
        try:
            with pytest.raises(RuntimeContractError, match="already has a live owner"):
                await owner.create_session(ROLE, workspace=workspace, member_id="member")
        finally:
            worker_released.set()
            fake_released.set()
        assert await turn == "done"
        await closing
        reopened = await owner.create_session(ROLE, workspace=workspace, member_id="member")
        assert reopened.session_key == session.session_key
        await owner.close()
        if runtime is not None:
            await runtime.workspaces.close()

    asyncio.run(scenario())
