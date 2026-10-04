"""Same-client continuation and reconstruction through the workspace API."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from io import StringIO
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel
from tests.support.run_execution import run_execution_record

from vs_agent.api import (
    NULL_AGENT_EVENT_SINK,
    AgentClient,
    AgentInvocationState,
    AgentSessionState,
    AgentSpec,
    AgentTurnRequest,
    Completed,
    DurableSessionStore,
)
from vs_agent.api.testing import FakeDriver
from vs_project.api import OrchestrationDescriptor, Project, RunEnvironmentRecord
from vs_prompts.api import TemplateRenderer
from vs_runtime.api import AgentCapability, AgentRole, WorkspaceAccess
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentExecutionScope,
    BlockingOperations,
    WorkspaceEvaluationSpec,
    create_run_control_channel,
    create_workspace_runtime,
)
from vs_runtime.api.testing import (
    FakeAgentExecutionEnvironment,
    FakeAgentExecutionLifecycleSink,
    FakeRunControlEventSink,
)
from vs_sandbox.api import ProjectPathPolicy, SandboxExecutionResult

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import (
        TrustedAccuracyResult,
        TrustedBenchmarkResult,
        WorkspaceRuntime,
    )
    from vs_sandbox.api import HostResource


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
        return AgentExecutionScope(
            workspace_path=self.path,
            log_directory=self.path,
            open_environment=lambda _: FakeAgentExecutionEnvironment(
                project_path_policy=ProjectPathPolicy()
            ),
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
    required_capabilities=frozenset({AgentCapability.DURABLE_TURN_CONTINUATION}),
)


def open_runtime(project: Project, path: Path, driver: FakeDriver) -> WorkspaceRuntime:
    namespace = project.state.local_namespace("run-1", "agent")

    def client(**kwargs: object) -> AgentClient:
        return AgentClient(
            driver,
            provider="fake",
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
        resolve_configuration=lambda _: AgentExecutionConfiguration("worker", AgentSpec()),
        session_store=lambda: None,
        invocation_store=lambda _: namespace.slot("invocations.json", AgentInvocationState),
        control=create_run_control_channel(FakeRunControlEventSink()),
        lifecycle_events=FakeAgentExecutionLifecycleSink(),
        agent_events=NULL_AGENT_EVENT_SINK,
        route_message=lambda message, _: message,
        blocking=BlockingOperations(),
        client_factory=client,
    )


def test_initial_turn_and_reconstructed_resume_use_the_same_conversation(tmp_path: Path) -> None:
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
    turns: list[AgentTurnRequest] = []
    initial_driver = FakeDriver(answer={"value": 7}, on_turn=turns.append)
    reconstructed_driver = FakeDriver(answer={"value": 8}, on_turn=turns.append)
    # A local template keeps the continuation at the checked agent-bound sink.
    template = tmp_path / "resume.j2"
    template.write_text("{{ result }}")
    message = TemplateRenderer(tmp_path).render_template("resume.j2", result="trusted result")

    async def scenario() -> None:
        runtime = open_runtime(project, tmp_path, initial_driver)
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
