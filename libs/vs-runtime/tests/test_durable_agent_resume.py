"""Same-client continuation and reconstruction through the workspace API."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, replace
from functools import partial
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel
from tests.support.run_execution import run_execution_record
from tests.support.runtime_agent_sessions import _OpenedSessionContract, _resume_transport

from vs_agent.api import (
    NULL_AGENT_EVENT_SINK,
    AgentCapabilities,
    AgentClient,
    AgentExecutionPolicy,
    AgentInvocationState,
    AgentOutputSchemaError,
    AgentSessionCheckpoint,
    AgentSessionKey,
    AgentSessionSpec,
    AgentSessionState,
    AgentSpec,
    AgentTurnRequest,
    ClientAgentSessions,
    Completed,
    DurableSessionStore,
    InvalidResponse,
    InvocationConflictError,
    InvocationOutcome,
    Pending,
    SessionPersistenceError,
    SessionResumeError,
    StdioServerDescriptor,
    Unknown,
)
from vs_agent.api.testing import FakeAgentInvocationStore, FakeDriver, FakeTurnScript
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import (
    AgentCapability,
    AgentRole,
    AgentSession,
    AgentTool,
    RuntimeContractError,
    StructuredResponseError,
    WorkspaceAccess,
)
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionFinished,
    AgentExecutionScope,
    AgentExecutionStarted,
    AgentExecutionStatus,
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
    from vs_agent.api import AgentInvocationStore
    from vs_prompts.api import RenderedPrompt
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
    *,
    invocation_store: AgentInvocationStore | None = None,
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
        invocation_store=lambda _: (
            invocation_store
            if invocation_store is not None
            else namespace.slot("invocations.json", AgentInvocationState)
        ),
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
        finished = [
            event
            for event in lifecycle.events
            if isinstance(event, AgentExecutionFinished) and event.execution_id == "resume-1"
        ]
        assert len(finished) == 1
        assert finished[0].result == Reply(value=7)
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


class CommitFailureStore(FakeAgentInvocationStore):
    def __init__(self, failing_save: int) -> None:
        super().__init__()
        self.failing_save = failing_save
        self.saves = 0

    def save(self, model: AgentInvocationState) -> None:
        self.saves += 1
        if self.saves == self.failing_save:
            detail = "injected invocation commit failure"
            raise OSError(detail)
        super().save(model)


@pytest.mark.parametrize("failing_save", [1, 2])
def test_resume_finishes_lifecycle_when_invocation_commit_fails(
    tmp_path: Path, failing_save: int
) -> None:
    lifecycle = FakeAgentExecutionLifecycleSink()
    turns: list[AgentTurnRequest] = []
    driver = FakeDriver(answer={"value": 7}, on_turn=turns.append)
    message = TemplateRenderer(tmp_path).render_string("Evaluation settled.")

    async def scenario() -> None:
        runtime = open_runtime(
            create_project(tmp_path),
            tmp_path,
            driver,
            lifecycle,
            invocation_store=CommitFailureStore(failing_save),
        )
        try:
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            await session.turn("initial", response=Reply)
            with pytest.raises(SessionPersistenceError, match="cannot commit invocation"):
                await session.resume(message, "resume-failed", response=Reply)
            events = [event for event in lifecycle.events if event.execution_id == "resume-failed"]
            assert len(events) == 2
            assert isinstance(events[0], AgentExecutionStarted)
            finished = events[1]
            assert isinstance(finished, AgentExecutionFinished)
            assert finished.status is AgentExecutionStatus.FAILED
            assert finished.error is not None
            assert "SessionPersistenceError" in finished.error
            assert "injected invocation commit failure" in finished.error
            assert len(turns) == failing_save
        finally:
            await runtime.workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("implementation", ["runtime", "fake"])
def test_initial_turn_journal_replays_after_host_reconstruction(
    tmp_path: Path, implementation: str
) -> None:
    create_project(tmp_path)
    calls: list[str] = []

    def respond(
        _role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> dict[str, int]:
        calls.append(message)
        return {"value": 7}

    async def scenario() -> None:
        for restarted in (False, True):
            project = Project.open(tmp_path)
            if implementation == "runtime":
                runtime = open_runtime(
                    project,
                    tmp_path,
                    FakeDriver(
                        answer={"value": 7}, on_turn=lambda turn: calls.append(turn.message)
                    ),
                )
                owner = runtime.agents
                workspace = runtime.workspaces.root
            else:
                runtime = None
                fake = FakeWorkspaceAgentSessions(
                    (ROLE,),
                    responder=respond,
                    supported_extra_tools={"diagnostic"},
                    supported_agent_capabilities={
                        AgentCapability.DURABLE_TURN_CONTINUATION,
                        AgentCapability.PROVIDER_SESSION_RESUME,
                        AgentCapability.MCP_SERVERS,
                    },
                )
                fake.bind_invocation_store(
                    project.state.local_namespace("run-1", "agent").slot(
                        "invocations.json", AgentInvocationState
                    )
                )
                owner = fake
                workspace = FakeWorkspace(path=tmp_path)
            session = await owner.create_session(ROLE, workspace=workspace, member_id="member")
            if restarted:
                assert isinstance(session.inspect("initial-1"), Completed)
            assert await session.turn(
                "initial", response=Reply, invocation_id="initial-1"
            ) == Reply(value=7)
            assert isinstance(session.inspect("initial-1"), Completed)
            assert calls == ["initial"]
            await owner.close()
            if runtime is not None:
                await runtime.workspaces.close()

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_parallel_fake_member_initial_turns_preserve_both_completions() -> None:
    entered = {name: asyncio.Event() for name in ("one", "two")}
    release = {name: asyncio.Event() for name in ("one", "two")}

    async def respond(
        _role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> str:
        entered[message].set()
        await release[message].wait()
        return message

    role = AgentRole(id="worker", system_prompt="Work.")
    owner = FakeWorkspaceAgentSessions(
        (role,),
        responder=respond,
        supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
    )
    ledger = FakeAgentInvocationStore()
    owner.bind_invocation_store(ledger)
    one = await owner.create_session(role, workspace=FakeWorkspace(), member_id="one")
    two = await owner.create_session(role, workspace=FakeWorkspace(), member_id="two")
    tasks = [asyncio.create_task(one.turn("one", invocation_id="one"))]
    try:
        await entered["one"].wait()
        tasks.append(asyncio.create_task(two.turn("two", invocation_id="two")))
        await entered["two"].wait()
        assert isinstance(one.inspect("one"), Pending)
        assert isinstance(two.inspect("two"), Pending)
        observer = FakeWorkspaceAgentSessions(
            (role,),
            responder=respond,
            supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
        )
        observer.bind_invocation_store(ledger)
        observed = await observer.create_session(role, workspace=FakeWorkspace(), member_id="one")
        assert isinstance(observed.inspect("one"), Unknown)
        await observer.close()
        release["one"].set()
        assert await tasks[0] == "one"
        release["two"].set()
        assert await tasks[1] == "two"
        assert isinstance(one.inspect("one"), Completed)
        assert isinstance(two.inspect("two"), Completed)
        assert one.inspect("one").checkpoint == one.checkpoint()
        assert two.inspect("two").checkpoint == two.checkpoint()
        restored = FakeWorkspaceAgentSessions(
            (role,),
            responder=respond,
            supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
        )
        restored.bind_invocation_store(ledger)
        reopened = await restored.create_session(role, workspace=FakeWorkspace(), member_id="one")
        assert isinstance(reopened.inspect("one"), Completed)
        await restored.close()
    finally:
        for gate in release.values():
            gate.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        await owner.close()


class AlternateReply(BaseModel):
    other: str


@pytest.mark.parametrize("implementation", ["runtime", "fake"])
def test_invalid_initial_reply_is_durable_and_allows_explicit_correction(
    tmp_path: Path,
    implementation: str,
) -> None:
    calls: list[str] = []
    role = ROLE

    async def scenario() -> None:
        if implementation == "runtime":
            runtime = open_runtime(
                create_project(tmp_path),
                tmp_path,
                FakeDriver(
                    answer={"other": "accepted"},
                    on_turn=lambda turn: calls.append(turn.message),
                ),
            )
            owner = runtime.agents
            workspace = runtime.workspaces.root
        else:
            runtime = None

            def respond(
                _role: AgentRole,
                _history: tuple[str, ...],
                message: str,
                _response: type[BaseModel] | None,
            ) -> dict[str, str]:
                calls.append(message)
                return {"other": "accepted"}

            owner = FakeWorkspaceAgentSessions(
                (role,),
                responder=respond,
                supported_extra_tools={"diagnostic"},
                supported_agent_capabilities={
                    AgentCapability.DURABLE_TURN_CONTINUATION,
                    AgentCapability.PROVIDER_SESSION_RESUME,
                    AgentCapability.MCP_SERVERS,
                },
            )
            workspace = FakeWorkspace()
        session = await owner.create_session(role, workspace=workspace, member_id="member")
        try:
            for _ in range(2):
                with pytest.raises(StructuredResponseError):
                    await session.turn("initial", response=Reply, invocation_id="invalid")
                assert isinstance(session.inspect("invalid"), Completed)
                assert calls == ["initial"]
            assert await session.turn(
                "correction",
                response=AlternateReply,
                invocation_id="correction",
            ) == AlternateReply(other="accepted")
            assert calls == ["initial", "correction"]
            assert isinstance(session.inspect("correction"), Completed)
        finally:
            await owner.close()
            if runtime is not None:
                await runtime.workspaces.close()

    asyncio.run(scenario())


@pytest.mark.asyncio
async def test_fake_initial_journal_uses_the_bound_provider_checkpoint(tmp_path: Path) -> None:
    transport, client = _resume_transport(tmp_path, lambda _: None)
    owner = FakeWorkspaceAgentSessions(
        (ROLE,),
        supported_extra_tools={"diagnostic"},
        supported_agent_capabilities={
            AgentCapability.DURABLE_TURN_CONTINUATION,
            AgentCapability.PROVIDER_SESSION_RESUME,
            AgentCapability.MCP_SERVERS,
        },
    )
    owner.bind_session_transport(transport)
    try:
        session = await owner.create_session(ROLE, workspace=FakeWorkspace(), member_id="member")
        checkpoint = session.checkpoint()
        assert await session.turn("initial", invocation_id="bound-initial") == "initial"
        outcome = session.inspect("bound-initial")
        assert isinstance(outcome, Completed)
        assert outcome.checkpoint == checkpoint == session.checkpoint()
    finally:
        await owner.close()
        client.close()


@pytest.mark.asyncio
async def test_fake_first_initial_establishes_checkpoint_but_later_missing_proof_fences_dispatch(
    tmp_path: Path,
) -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    key = AgentSessionKey.for_member(role.id, "member")
    spec = AgentSessionSpec(
        role=role.id,
        provider="fake",
        workspace=tmp_path,
        policy=AgentExecutionPolicy(require_enforcement=False),
    )
    client = AgentClient(FakeDriver(answer="provider checkpoint established"))
    ledger = FakeAgentInvocationStore()
    transport = ClientAgentSessions(client, ledger)
    calls: list[str] = []

    def respond(
        _role: AgentRole, _history: tuple[str, ...], message: str, _response: type[BaseModel] | None
    ) -> str:
        calls.append(message)
        client.run(
            session_spec=spec,
            turn=AgentTurnRequest(message, require_provider_checkpoint=True),
            session_key=key,
        )
        return message

    owner = FakeWorkspaceAgentSessions(
        (role,),
        responder=respond,
        supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
    )
    owner.bind_session_transport(transport)
    session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
    try:
        with pytest.raises(SessionResumeError, match="provider checkpoint is missing"):
            session.checkpoint()
        assert await session.turn("first", invocation_id="initial") == "first"
        first = session.inspect("initial")
        assert isinstance(first, Completed)
        assert first.checkpoint == session.checkpoint()
        client.close()
        with pytest.raises(SessionResumeError, match="provider checkpoint is missing"):
            await session.turn("later", invocation_id="later")
        assert calls == ["first"]
        current = ledger.load_optional()
        assert current is not None
        assert set(current.invocations) == {"initial"}
    finally:
        await owner.close()
        client.close()


@pytest.mark.asyncio
async def test_fake_inspection_before_initial_dispatch_is_unknown() -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    owner = FakeWorkspaceAgentSessions(
        (role,),
        supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
    )
    try:
        session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
        assert isinstance(session.inspect("not-dispatched"), Unknown)
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_fake_provider_schema_rejection_without_checkpoint_fences_all_corrections() -> None:
    calls: list[str] = []

    def respond(
        _role: AgentRole,
        _history: tuple[str, ...],
        message: str,
        _response: type[BaseModel] | None,
    ) -> dict[str, int]:
        calls.append(message)
        if message == "initial":
            raise AgentOutputSchemaError(detail="provider rejected its response")
        return {"value": 7}

    role = AgentRole(id="worker", system_prompt="Work.")
    ledger = FakeAgentInvocationStore()
    owner = FakeWorkspaceAgentSessions(
        (role,),
        responder=respond,
        supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
    )
    owner.bind_invocation_store(ledger)
    session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
    try:
        for _ in range(2):
            with pytest.raises(StructuredResponseError, match="provider rejected"):
                await session.turn("initial", response=Reply, invocation_id="initial")
            outcome = session.inspect("initial")
            assert isinstance(outcome, InvalidResponse)
            assert outcome.checkpoint is None
            assert calls == ["initial"]
        restarted = FakeWorkspaceAgentSessions(
            (role,),
            responder=respond,
            supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
        )
        restarted.bind_invocation_store(ledger)
        reopened = await restarted.create_session(
            role, workspace=FakeWorkspace(), member_id="member"
        )
        try:
            with pytest.raises(SessionResumeError, match="provider checkpoint is missing"):
                await reopened.turn("correction", response=Reply, invocation_id="correction")
            assert calls == ["initial"]
        finally:
            await restarted.close()
        with pytest.raises(SessionResumeError, match="provider checkpoint is missing"):
            await session.turn("correction", response=Reply, invocation_id="correction")
        assert calls == ["initial"]
        assert isinstance(session.inspect("correction"), Unknown)
    finally:
        await owner.close()


@pytest.mark.asyncio
async def test_fake_predispatch_interrupt_has_no_unresolved_invocation() -> None:
    role = AgentRole(id="worker", system_prompt="Work.")
    owner = FakeWorkspaceAgentSessions(
        (role,),
        supported_agent_capabilities={AgentCapability.PROVIDER_SESSION_RESUME},
    )
    try:
        session = await owner.create_session(role, workspace=FakeWorkspace(), member_id="member")
        task = asyncio.create_task(session.turn("interrupted", invocation_id="interrupted"))
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        session.release_interrupted("interrupted")
        assert isinstance(session.inspect("interrupted"), Unknown)
        assert await session.turn("replacement", invocation_id="replacement") == "replacement"
        assert isinstance(session.inspect("replacement"), Completed)
    finally:
        await owner.close()


def _record_fence_response(
    calls: list[str],
    _role: AgentRole,
    _history: tuple[str, ...],
    text: str,
    _response: type[BaseModel] | None,
) -> str:
    calls.append(text)
    return text


@pytest.mark.parametrize("implementation", ["runtime", "fake"])
def test_initial_and_resume_share_unknown_fences_after_reconstruction(
    tmp_path: Path, implementation: str
) -> None:
    """Both dispatch paths preserve the other path's unresolved ownership."""
    project = create_project(tmp_path)
    calls: list[str] = []
    message = TemplateRenderer(tmp_path).render_string("trusted result")
    ledger = project.state.local_namespace("run-1", "agent").slot(
        "invocations.json", AgentInvocationState
    )

    def fail(turn: AgentTurnRequest) -> None:
        calls.append(turn.message)
        if turn.invocation_id == "unknown":
            detail = "lost provider acceptance"
            raise OSError(detail)

    async def scenario() -> None:
        transport_client = None
        transport = None
        if implementation == "fake":
            transport_path = tmp_path / "transport"
            transport_path.mkdir()
            transport, transport_client = _resume_transport(transport_path, fail)
            calls.clear()
        try:
            for restarted in (False, True):
                if implementation == "runtime":
                    runtime = open_runtime(
                        project, tmp_path, FakeDriver(answer="done", on_turn=fail)
                    )
                    owner = runtime.agents
                    workspace = runtime.workspaces.root
                else:
                    runtime = None
                    owner = FakeWorkspaceAgentSessions(
                        (ROLE,),
                        responder=partial(_record_fence_response, calls),
                        supported_extra_tools={"diagnostic"},
                        supported_agent_capabilities={
                            AgentCapability.PROVIDER_SESSION_RESUME,
                            AgentCapability.DURABLE_TURN_CONTINUATION,
                            AgentCapability.MCP_SERVERS,
                        },
                    )
                    owner.bind_invocation_store(ledger)
                    assert transport is not None
                    owner.bind_session_transport(transport)
                    workspace = FakeWorkspace(path=tmp_path)
                opened = _OpenedSessionContract(owner, (), runtime)
                try:
                    session = await owner.create_session(
                        ROLE, workspace=workspace, member_id="member"
                    )
                    if not restarted:
                        await session.turn("initial", invocation_id="initial")
                        assert isinstance(await session.resume(message, "unknown"), Unknown)
                    assert isinstance(session.inspect("unknown"), Unknown)
                    before = tuple(calls)
                    with pytest.raises(SessionResumeError, match="lost provider acceptance"):
                        await session.turn("new initial", invocation_id="new-initial")
                    assert isinstance(await session.resume(message, "unknown"), Unknown)
                    assert tuple(calls) == before
                finally:
                    await opened.close()
        finally:
            if transport_client is not None:
                transport_client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("transport_first", [False, True])
@pytest.mark.parametrize("shared_store", [False, True])
def test_fake_transport_journal_owns_both_turn_paths_regardless_of_binding_order(
    tmp_path: Path, *, transport_first: bool, shared_store: bool
) -> None:
    def fail(turn: AgentTurnRequest) -> None:
        if turn.invocation_id == "unknown":
            detail = "lost acknowledgement"
            raise OSError(detail)

    transport, client = _resume_transport(tmp_path, fail)
    with transport.invocation_transaction() as store:
        supplied = store if shared_store else FakeAgentInvocationStore()
    message = TemplateRenderer(tmp_path).render_string("trusted result")

    async def scenario() -> None:
        owner = FakeWorkspaceAgentSessions(
            (ROLE,),
            supported_extra_tools={"diagnostic"},
            supported_agent_capabilities={
                AgentCapability.PROVIDER_SESSION_RESUME,
                AgentCapability.DURABLE_TURN_CONTINUATION,
                AgentCapability.MCP_SERVERS,
            },
        )
        if transport_first:
            owner.bind_session_transport(transport)
            owner.bind_invocation_store(supplied)
        else:
            owner.bind_invocation_store(supplied)
            owner.bind_session_transport(transport)
        try:
            session = await owner.create_session(
                ROLE, workspace=FakeWorkspace(), member_id="member"
            )
            await session.turn("initial", invocation_id="initial")
            assert isinstance(await session.resume(message, "unknown"), Unknown)
            with transport.invocation_transaction() as store:
                state = store.load_optional()
            assert state is not None
            assert isinstance(state.invocations["initial"].outcome, Completed)
            assert isinstance(state.invocations["unknown"].outcome, Unknown)
            with pytest.raises(SessionResumeError, match="lost acknowledgement"):
                await session.turn("replacement", invocation_id="replacement")
            if not shared_store:
                assert supplied.load_optional() is None
        finally:
            await owner.close()
            client.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("provider_rejection", [False, True])
def test_correction_preserves_checkpoint_when_provider_would_retire_unrestricted_turn(
    tmp_path: Path, *, provider_rejection: bool
) -> None:
    turns: list[AgentTurnRequest] = []
    invalid = (
        AgentOutputSchemaError("missing value")
        if provider_rejection
        else {"other": "missing value"}
    )
    driver = FakeDriver(
        script=FakeTurnScript((invalid, {"value": 7}), reset_after_turn=2), on_turn=turns.append
    )
    project = create_project(tmp_path)
    correction = TemplateRenderer(tmp_path).render_string("Please correct the response.")

    async def scenario() -> None:
        runtime = open_runtime(project, tmp_path, driver)
        try:
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            with pytest.raises(StructuredResponseError):
                await session.turn("initial", response=Reply, invocation_id="initial")
            if provider_rejection:
                with pytest.raises(SessionResumeError, match="provider checkpoint is missing"):
                    await session.resume(correction, "initial/correction", response=Reply)
                assert len(turns) == 1
                return
            checkpoint = session.checkpoint()
            outcome = await session.resume(correction, "initial/correction", response=Reply)
            assert isinstance(outcome, Completed)
            assert Reply.model_validate_json(outcome.result.text) == Reply(value=7)
            assert outcome.checkpoint == checkpoint == session.checkpoint()
            assert turns[-1].expected_provider_session_id == checkpoint.provider_session_id
            assert session.inspect("initial/correction") == outcome
        finally:
            await runtime.workspaces.close()

    asyncio.run(scenario())


def _damage_correction_checkpoint(project: Project, state_name: str) -> None:
    if state_name == "present":
        return
    slot = project.state.local_namespace("run-1", "agent").slot("sessions.json", AgentSessionState)
    state = slot.load_optional()
    if state is None:
        return
    if state_name == "lost":
        state.sessions.clear()
    else:
        state.sessions = {
            key: record.model_copy(update={"session_id": "replaced-conversation"})
            for key, record in state.sessions.items()
        }
    slot.save(state)


async def _observe_correction(
    session: AgentSession, message: RenderedPrompt
) -> InvocationOutcome | None:
    failure = None
    outcome = None
    try:
        outcome = await session.resume(message, "initial/correction", response=Reply)
    except SessionResumeError as error:
        failure = error
    if failure is not None:
        assert "unresolved" not in failure.detail
        assert any(word in failure.detail for word in ("checkpoint", "conversation"))
    return outcome


def _assert_correction_outcome(
    outcome: InvocationOutcome | None, first: InvocationOutcome, initial: str, correction: str
) -> None:
    if isinstance(outcome, Completed):
        assert initial != "provider-invalid"
        assert correction in ("valid", "invalid")
        assert outcome.checkpoint == first.checkpoint
        if correction == "valid":
            assert Reply.model_validate_json(outcome.result.text) == Reply(value=7)
    elif outcome is not None:
        assert isinstance(outcome, (InvalidResponse, Unknown))
        assert "unresolved" not in outcome.detail
        assert any(
            word in outcome.detail for word in ("checkpoint", "conversation", "provider", "schema")
        )


@settings(max_examples=144)
@example(initial="invalid", correction="valid", checkpoint="present", restart="after")
@example(initial="provider-invalid", correction="valid", checkpoint="lost", restart="before")
@example(initial="valid", correction="valid", checkpoint="reset", restart="before")
@example(initial="invalid", correction="provider-invalid", checkpoint="present", restart="before")
@example(initial="external-failure", correction="valid", checkpoint="present", restart="before")
@given(
    initial=st.sampled_from(("valid", "invalid", "provider-invalid", "external-failure")),
    correction=st.sampled_from(("valid", "invalid", "provider-invalid", "external-failure")),
    checkpoint=st.sampled_from(("present", "lost", "reset")),
    restart=st.sampled_from(("never", "before", "after")),
)
def test_generated_correction_checkpoint_and_restart_outcomes(
    initial: str, correction: str, checkpoint: str, restart: str
) -> None:
    """Correction evidence survives faults without provider replay or vague failures."""
    with TemporaryDirectory(prefix="loopfix4-properties-") as directory:
        path = Path(directory)
        project = create_project(path)
        turns: list[AgentTurnRequest] = []
        answers = {
            "valid": {"value": 7},
            "invalid": {"other": "missing value"},
            "provider-invalid": AgentOutputSchemaError("provider schema rejected value"),
            "external-failure": {"value": 7},
        }

        def observe(turn: AgentTurnRequest) -> None:
            turns.append(turn)
            failing_turn = (initial == "external-failure" and turn.invocation_id == "initial") or (
                correction == "external-failure" and turn.invocation_id == "initial/correction"
            )
            if failing_turn:
                detail = "provider acknowledgement was lost"
                raise OSError(detail)

        message = TemplateRenderer(path).render_string("Please correct the response.")

        async def scenario() -> None:
            runtime = open_runtime(
                project,
                path,
                FakeDriver(
                    script=FakeTurnScript((answers[initial], answers[correction])), on_turn=observe
                ),
            )
            try:
                session = await runtime.agents.create_session(
                    ROLE, workspace=runtime.workspaces.root, member_id="member"
                )
                if initial == "valid":
                    assert await session.turn(
                        "initial", response=Reply, invocation_id="initial"
                    ) == Reply(value=7)
                elif initial == "external-failure":
                    with pytest.raises(
                        SessionResumeError, match="provider acknowledgement was lost"
                    ):
                        await session.turn("initial", response=Reply, invocation_id="initial")
                else:
                    with pytest.raises(StructuredResponseError):
                        await session.turn("initial", response=Reply, invocation_id="initial")
                first = session.inspect("initial")
                _damage_correction_checkpoint(project, checkpoint)
                if restart == "before":
                    await runtime.workspaces.close()
                    runtime = open_runtime(
                        project,
                        path,
                        FakeDriver(script=FakeTurnScript((answers[correction],)), on_turn=observe),
                    )
                    session = await runtime.agents.create_session(
                        ROLE, workspace=runtime.workspaces.root, member_id="member"
                    )
                outcome = await _observe_correction(session, message)
                _assert_correction_outcome(outcome, first, initial, correction)
                accepted = len(turns)
                recorded = session.inspect("initial/correction")
                if restart == "after":
                    await runtime.workspaces.close()
                    runtime = open_runtime(
                        project, path, FakeDriver(answer={"value": 99}, on_turn=observe)
                    )
                    session = await runtime.agents.create_session(
                        ROLE, workspace=runtime.workspaces.root, member_id="member"
                    )
                assert session.inspect("initial/correction") == recorded
                replayed = await _observe_correction(session, message)
                assert replayed == outcome
                assert len(turns) == accepted
                assert sum(turn.invocation_id == "initial" for turn in turns) == 1
            finally:
                await runtime.workspaces.close()

        asyncio.run(scenario())


def test_journaled_turn_suppresses_discretionary_checkpoint_retirement(tmp_path: Path) -> None:
    driver = FakeDriver(script=FakeTurnScript(({"value": 7},), reset_after_turn=1))
    message = TemplateRenderer(tmp_path).render_string("Continue with settled evidence.")

    async def scenario() -> None:
        runtime = open_runtime(create_project(tmp_path), tmp_path, driver)
        try:
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            assert await session.turn("initial", response=Reply, invocation_id="retained") == Reply(
                value=7
            )
            outcome = session.inspect("retained")
            assert isinstance(outcome, Completed)
            checkpoint = session.checkpoint()
            assert outcome.checkpoint == checkpoint
            resumed = await session.resume(message, "retained/resume", response=Reply)
            assert isinstance(resumed, Completed)
            assert resumed.checkpoint == checkpoint
            assert Reply.model_validate_json(resumed.result.text) == Reply(value=7)
        finally:
            await runtime.workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("legacy", [False, True])
def test_latest_initial_checkpoint_follows_dispatch_order_not_sorted_identity(
    tmp_path: Path, *, legacy: bool
) -> None:
    project = create_project(tmp_path)
    turns: list[AgentTurnRequest] = []
    message = TemplateRenderer(tmp_path).render_string("Please correct the response.")

    async def scenario() -> None:
        runtime = open_runtime(
            project, tmp_path, FakeDriver(answer={"value": 1}, on_turn=turns.append)
        )
        try:
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            assert await session.turn("old", response=Reply, invocation_id="z-earlier") == Reply(
                value=1
            )
            await runtime.workspaces.close()
            runtime = open_runtime(
                project, tmp_path, FakeDriver(answer={"value": 2}, on_turn=turns.append)
            )
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            assert await session.turn("new", response=Reply, invocation_id="a-newer") == Reply(
                value=2
            )
            newer = session.checkpoint()
            # A recovered journal may contain an older conversation's accepted
            # result. Keep current proof intact and make chronological selection
            # observable without replacing the live provider conversation.
            slot = project.state.local_namespace("run-1", "agent").slot(
                "invocations.json", AgentInvocationState
            )
            state = slot.load_optional()
            assert state is not None
            earlier = state.invocations["z-earlier"]
            assert isinstance(earlier.outcome, Completed)
            older = AgentSessionCheckpoint(
                session_key=str(session.session_key), provider_session_id="older-conversation"
            )
            state.invocations["z-earlier"] = earlier.model_copy(
                update={
                    "outcome": Completed(
                        session_key=str(session.session_key),
                        invocation_id="z-earlier",
                        checkpoint=older,
                        result=replace(
                            earlier.outcome.result, provider_session_id=older.provider_session_id
                        ),
                    )
                }
            )
            slot.save(state)
            if legacy:
                _restore_legacy_invocation_document(project)
                with pytest.raises(SessionResumeError, match="checkpoint identity changed"):
                    await session.resume(message, "a-newer/correction", response=Reply)
                assert len(turns) == 2
                return
            corrected = await session.resume(message, "a-newer/correction", response=Reply)
            assert isinstance(corrected, Completed)
            assert corrected.checkpoint == newer
            assert Reply.model_validate_json(corrected.result.text) == Reply(value=2)
            await runtime.workspaces.close()
            runtime = open_runtime(
                project, tmp_path, FakeDriver(answer={"value": 3}, on_turn=turns.append)
            )
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            assert await session.resume(message, "a-newer/correction", response=Reply) == corrected
            assert [turn.invocation_id for turn in turns] == [
                "z-earlier",
                "a-newer",
                "a-newer/correction",
            ]
        finally:
            await runtime.workspaces.close()

    asyncio.run(scenario())


def _restore_legacy_invocation_document(project: Project) -> None:
    slot = project.state.local_namespace("run-1", "agent").slot(
        "invocations.json", AgentInvocationState
    )
    state = slot.load_optional()
    assert state is not None
    document = state.model_dump()
    for record in document["invocations"].values():
        record.pop("sequence", None)
    slot.save(AgentInvocationState.model_validate(document))


def test_legacy_single_checkpoint_allows_correction_without_replaying_initial(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path)
    turns: list[AgentTurnRequest] = []
    message = TemplateRenderer(tmp_path).render_string("Please correct the response.")

    async def scenario() -> None:
        runtime = open_runtime(
            project, tmp_path, FakeDriver(answer={"other": "invalid"}, on_turn=turns.append)
        )
        try:
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            with pytest.raises(StructuredResponseError):
                await session.turn("initial", response=Reply, invocation_id="initial")
            checkpoint = session.checkpoint()
            await runtime.workspaces.close()
            _restore_legacy_invocation_document(project)
            runtime = open_runtime(
                project, tmp_path, FakeDriver(answer={"value": 7}, on_turn=turns.append)
            )
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            outcome = await session.resume(message, "initial/correction", response=Reply)
            assert isinstance(outcome, Completed)
            assert outcome.checkpoint == checkpoint
            assert Reply.model_validate_json(outcome.result.text) == Reply(value=7)
            assert [turn.invocation_id for turn in turns] == ["initial", "initial/correction"]
        finally:
            await runtime.workspaces.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("restart", [False, True])
def test_failed_initial_with_prior_checkpoint_preserves_provider_failure(
    tmp_path: Path, *, restart: bool
) -> None:
    project = create_project(tmp_path)
    turns: list[AgentTurnRequest] = []
    message = TemplateRenderer(tmp_path).render_string("Please correct the response.")

    def observe(turn: AgentTurnRequest) -> None:
        turns.append(turn)
        if turn.invocation_id == "failed-initial":
            detail = "provider acknowledgement was lost"
            raise OSError(detail)

    async def scenario() -> None:
        runtime = open_runtime(project, tmp_path, FakeDriver(answer={"value": 7}, on_turn=observe))
        try:
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            await session.turn("warmup", response=Reply, invocation_id="warmup")
            with pytest.raises(
                SessionResumeError, match=r"OSError.*provider acknowledgement was lost"
            ):
                await session.turn("failed", response=Reply, invocation_id="failed-initial")
            if restart:
                await runtime.workspaces.close()
                runtime = open_runtime(
                    project, tmp_path, FakeDriver(answer={"value": 9}, on_turn=observe)
                )
                session = await runtime.agents.create_session(
                    ROLE, workspace=runtime.workspaces.root, member_id="member"
                )
            for _ in range(2):
                with pytest.raises(
                    SessionResumeError, match=r"OSError.*provider acknowledgement was lost"
                ):
                    await session.resume(message, "failed-initial/correction", response=Reply)
            assert [turn.invocation_id for turn in turns] == ["warmup", "failed-initial"]
        finally:
            await runtime.workspaces.close()

    asyncio.run(scenario())


class TextReply(BaseModel):
    text: str


class FlagReply(BaseModel):
    enabled: bool


@pytest.mark.parametrize(
    "schemas",
    [
        (Reply, TextReply, FlagReply),
        (TextReply, FlagReply, Reply),
        (FlagReply, Reply, TextReply),
    ],
)
def test_successive_corrections_use_requested_schema_and_replay_after_restart(
    tmp_path: Path, schemas: tuple[type[BaseModel], ...]
) -> None:
    project = create_project(tmp_path)
    turns: list[AgentTurnRequest] = []
    replies = {Reply: {"value": 7}, TextReply: {"text": "accepted"}, FlagReply: {"enabled": True}}
    answers = [replies[schemas[0]]]
    for schema in schemas[1:]:
        answers.extend(({}, replies[schema]))
    driver = FakeDriver(script=FakeTurnScript(tuple(answers)), on_turn=turns.append)
    message = TemplateRenderer(tmp_path).render_string("Please correct the response.")
    corrections: list[Completed] = []

    async def scenario() -> None:
        runtime = open_runtime(project, tmp_path, driver)
        try:
            session = await runtime.agents.create_session(
                ROLE, workspace=runtime.workspaces.root, member_id="member"
            )
            await session.turn("warmup", response=schemas[0], invocation_id="warmup")
            for index, schema in enumerate(schemas[1:], start=1):
                with pytest.raises(StructuredResponseError):
                    await session.turn("work", response=schema, invocation_id=f"work-{index}")
                outcome = await session.resume(message, f"work-{index}/correction", response=schema)
                assert turns[-1].output_schema is schema
                assert isinstance(outcome, Completed)
                assert schema.model_validate_json(outcome.result.text) == schema(**replies[schema])
                corrections.append(outcome)
        finally:
            await runtime.workspaces.close()

        restarted = open_runtime(project, tmp_path, driver)
        try:
            session = await restarted.agents.create_session(
                ROLE, workspace=restarted.workspaces.root, member_id="member"
            )
            for index, schema in enumerate(schemas[1:], start=1):
                assert (
                    await session.resume(message, f"work-{index}/correction", response=schema)
                    == corrections[index - 1]
                )
            assert [turn.output_schema for turn in turns] == [
                schemas[0],
                schemas[1],
                schemas[1],
                schemas[2],
                schemas[2],
            ]
        finally:
            await restarted.workspaces.close()

    asyncio.run(scenario())
