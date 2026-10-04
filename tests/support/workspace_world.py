"""A real Git-backed run workspace and restartable hosts, for workspace executor cases.

``open_workspace_env`` builds one run's disk state; ``WorkspaceEnv.start_host`` starts
another ``RuntimeWorkspaces`` over the same disk, as after a process restart. Set
``VIBESYS_STATE_HOME`` to a temporary directory before opening one.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.support.run_execution import run_execution_record

from vs_agent.api import NULL_AGENT_EVENT_SINK, NULL_SKILL_SELECTION
from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
)
from vs_runtime.api.infrastructure import (
    AgentPaths,
    BlockingOperations,
    ProjectRunEffects,
    ProjectRunRequest,
    RunEnvironmentRequest,
    RunEnvironmentView,
    RuntimeWorkspaces,
    TrustedEvaluationPlan,
    WorkspaceResourceFactory,
    create_run_control_channel,
    create_workspace_runtime,
    open_project_run_resources,
    open_run_environment_resources,
)
from vs_runtime.api.testing import FakeAgentExecutionLifecycleSink, FakeRunControlEventSink
from vs_sandbox.api import ProjectPathPolicy
from vs_sandbox.api.testing import FakeComputeBackend, FakeSandbox

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from typing import TextIO

    from vs_agent.api import AgentClientProtocol
    from vs_project.api import OrchestrationRunManifest
    from vs_runtime.api import AgentRole, OrchestrationResumeDecision
    from vs_runtime.api.infrastructure import (
        AgentExecutionConfiguration,
        WorkspaceResourceProvider,
    )
    from vs_sandbox.api import Sandbox

RUN_ID = "workspace-requests"


@dataclass
class _Session:
    sandbox: Sandbox = field(default_factory=FakeSandbox)
    view: RunEnvironmentView = field(
        default_factory=lambda: RunEnvironmentView(
            paths=AgentPaths(), supports_parallel_candidate_evaluation=True
        )
    )

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb

    def close(self) -> None:
        return None


def _emit(text: str, writer: TextIO) -> None:
    writer.write(text + "\n")
    writer.flush()


def _unexpected_execution(_role: AgentRole) -> AgentExecutionConfiguration:
    message = "workspace-only world opened an agent execution"
    raise AssertionError(message)


def _unexpected_client(**_kwargs: object) -> AgentClientProtocol:
    message = "workspace-only world opened an agent client"
    raise AssertionError(message)


@dataclass
class WorkspaceEnv:
    """One run's disk state and the hosts (RuntimeWorkspaces) that operate on it."""

    project: Project
    factory: WorkspaceResourceProvider
    hosts: list[RuntimeWorkspaces] = field(default_factory=list)

    def start_host(self) -> RuntimeWorkspaces:
        """Start another host over the same disk state, as after a process restart."""
        runtime = create_workspace_runtime(
            (),
            workspace_resources=self.factory,
            resolve_configuration=_unexpected_execution,
            session_store=lambda: None,
            control=create_run_control_channel(FakeRunControlEventSink()),
            lifecycle_events=FakeAgentExecutionLifecycleSink(),
            agent_events=NULL_AGENT_EVENT_SINK,
            route_message=lambda message, _steering: message,
            blocking=BlockingOperations(),
            client_factory=_unexpected_client,
        )
        self.hosts.append(runtime.workspaces)
        return runtime.workspaces

    def receipts_namespace(self):  # noqa: ANN201  # lint-waiver: LW-0D3-11 [ANN201]; the namespace type is vs_project's own.
        """The machine-local namespace that holds this run's executor receipts."""
        return self.project.state.local_namespace(RUN_ID, "receipts")


@contextmanager
def open_workspace_env(tmp_path: Path) -> Iterator[WorkspaceEnv]:
    """A fresh run with one started host. The caller closes ``env.hosts`` (async) before exit."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    request = ProjectRunRequest(
        project_root=root,
        run_id=RUN_ID,
        display_name="workspace requests",
        task_name=None,
        existing=False,
        framework_version="1.2.3",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="test-policy", config_version=1, options={}),
    )

    def unexpected_resume(_manifest: OrchestrationRunManifest) -> OrchestrationResumeDecision:
        message = "fresh run resumed"
        raise AssertionError(message)

    effects = ProjectRunEffects(
        git_events=NullGitTrackerEvents(), log_emit=_emit, on_log_ready=lambda _path: None
    )
    with open_project_run_resources(
        request, effects=effects, resolve_resume=unexpected_resume
    ) as project:
        environment = open_run_environment_resources(
            RunEnvironmentRequest(
                log_dir=project.logger.log_dir,
                workspace=root,
                ref_dir=None,
                backend=FakeComputeBackend(),
                agent_backend="stub",
                cli_provider="codex",
                run_id=RUN_ID,
                framework_root=tmp_path,
                project_path_policy=ProjectPathPolicy(),
                git_history_root=project.git.history_root,
            ),
            lambda _request: _Session(),
        )
        with closing(environment):
            factory = WorkspaceResourceFactory(
                project,
                environment,
                evaluation_plan=TrustedEvaluationPlan(),
                memory_paths=(),
                skill_source_dirs=(),
                skill_selection=NULL_SKILL_SELECTION,
                host_resources=(),
                events=lambda _event: None,
            )
            env = WorkspaceEnv(project.project, factory)
            env.start_host()
            yield env
