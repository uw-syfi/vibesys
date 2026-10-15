"""A real Git-backed run workspace and restartable hosts, for workspace executor cases.

``open_workspace_env`` builds one run's disk state; ``WorkspaceEnv.start_host`` starts
another ``RuntimeWorkspaces`` over the same disk, as after a process restart. Set
``VIBESYS_STATE_HOME`` to a temporary directory before opening one.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from tests.support.real_git import REAL_GIT

from vs_agent.api import NULL_SKILL_SELECTION
from vs_project.api import (
    Git,
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
    git_repositories,
)
from vs_project.api.testing import FakeGitRepositories, GitKind, run_execution_record
from vs_runtime.api.infrastructure import (
    AgentPaths,
    BlockingOperations,
    EnvironmentSystem,
    ProjectRunEffects,
    ProjectRunRequest,
    RunEnvironmentRequest,
    RunEnvironmentView,
    RuntimeWorkspaces,
    TrustedEvaluationPlan,
    WorkspaceResourceFactory,
    create_workspace_runtime,
    open_project_run_resources,
    open_run_environment_resources,
)
from vs_runtime.api.testing import MemoryValidationPasses, idle_environment_system
from vs_sandbox.api import ProjectPathPolicy
from vs_sandbox.api.testing import FakeCommandRunner, FakeComputeBackend
from vs_sim.api import OsThreads, ThreadBlockingRunner
from vs_sim.api.testing import ManualClock, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path
    from typing import TextIO

    from vs_agent.api import AgentClientProtocol
    from vs_project.api import (
        GitRepository,
        GitRepositoryFactory,
        OrchestrationRunManifest,
        StateNamespace,
    )
    from vs_runtime.api import AgentRole, OrchestrationResumeDecision
    from vs_runtime.api.infrastructure import (
        AgentExecutionConfiguration,
        RunEnvironmentSession,
        WorkspaceResourceEventSink,
        WorkspaceResourceProvider,
    )
    from vs_sandbox.api import CommandRunner, ComputeBackendImpl
    from vs_sim.api import BlockingRunner

RUN_ID = "workspace-requests"


@dataclass
class _Session:
    sandbox: CommandRunner = field(default_factory=FakeCommandRunner)
    view: RunEnvironmentView = field(
        default_factory=lambda: RunEnvironmentView(paths=AgentPaths(), cli_sandboxed=True)
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
    git_disk: FakeGitRepositories | Git = REAL_GIT
    """The in-memory repositories of a world on ``GitKind.FAKE``; ``REAL_GIT`` on real Git."""

    def start_host(self, runner: BlockingRunner | None = None) -> RuntimeWorkspaces:
        """Start another host over the same disk state, as after a process restart.

        *runner* runs the host's blocking operations (worker threads by default).
        """
        runtime = create_workspace_runtime(
            workspace_resources=self.factory,
            blocking=BlockingOperations(runner or ThreadBlockingRunner()),
            validation_passes=MemoryValidationPasses(),
        )
        self.hosts.append(runtime.workspaces)
        return runtime.workspaces

    def receipts_namespace(self) -> StateNamespace:
        """The machine-local namespace that holds this run's executor receipts."""
        return self.project.state.local_namespace(RUN_ID, "receipts")


def _git_factory(
    disk: FakeGitRepositories | Git, wrap: Callable[[GitRepository], GitRepository] | None
) -> GitRepositoryFactory:
    if isinstance(disk, Git):
        return git_repositories(disk)
    if wrap is None:
        return disk.repository
    return lambda root, faults: wrap(disk.repository(root, faults))


@contextmanager
def open_workspace_env(  # noqa: PLR0913  # lint-waiver: LW-402330 [PLR0913]; each option is an independent world setting and a bundling options type would only rename the keywords every caller passes.
    tmp_path: Path,
    *,
    git: GitKind = GitKind.FAKE,
    backend: ComputeBackendImpl | None = None,
    open_session: Callable[[RunEnvironmentRequest], RunEnvironmentSession] | None = None,
    owns_candidate_worktrees: bool = False,
    memory_paths: tuple[str, ...] = (),
    wrap_git: Callable[[GitRepository], GitRepository] | None = None,
    events: WorkspaceResourceEventSink | None = None,
    system: EnvironmentSystem | None = None,
) -> Iterator[WorkspaceEnv]:
    """A fresh run with one started host. The caller closes ``env.hosts`` (async) before exit.

    ``git`` selects the repository implementation: the in-memory Fake spawns no process;
    ``GitKind.REAL`` runs real Git. ``backend`` is the compute backend the requests carry.
    ``open_session`` opens every session (the root's and
    each candidate's) instead of the default fake one. ``owns_candidate_worktrees`` names
    the run's candidate worktrees directory in the request's ``run_owned_roots``, as a
    product run does. ``wrap_git`` wraps every Fake repository (not with real Git), so a
    case can stand between the run and Git, for example to kill the host at a Git step.
    ``events`` observes workspace resource events (for example a reported baseline drift);
    the default discards them. ``system`` is the host the environments run on; the default
    starts nothing (:func:`~vs_runtime.api.testing.idle_environment_system`).
    """
    disk = FakeGitRepositories() if git is GitKind.FAKE else REAL_GIT
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
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="test-policy", config_version=1, options={}),
    )

    def unexpected_resume(_manifest: OrchestrationRunManifest) -> OrchestrationResumeDecision:
        message = "fresh run resumed"
        raise AssertionError(message)

    effects = ProjectRunEffects(
        git_events=NullGitTrackerEvents(),
        log_emit=_emit,
        on_log_ready=lambda _path: None,
        git_repository=_git_factory(disk, wrap_git),
        clock=ManualClock(),
    )
    with open_project_run_resources(
        request, effects=effects, resolve_resume=unexpected_resume
    ) as project:
        environment = open_run_environment_resources(
            RunEnvironmentRequest(
                log_dir=project.logger.log_dir,
                workspace=root,
                ref_dir=None,
                backend=backend
                if backend is not None
                else FakeComputeBackend(threads=SimThreads()),
                agent_backend="stub",
                cli_provider="codex",
                run_id=RUN_ID,
                framework_root=tmp_path,
                project_path_policy=ProjectPathPolicy(),
                git_history_root=project.git.history_root,
                run_owned_roots=(
                    (project.project.state.candidate_worktrees_directory(RUN_ID),)
                    if owns_candidate_worktrees
                    else ()
                ),
                clock=ManualClock(),
                system=idle_environment_system() if system is None else system,
            ),
            open_session if open_session is not None else lambda _request: _Session(),
            clock=ManualClock(),
        )
        with closing(environment):
            factory = WorkspaceResourceFactory(
                project,
                environment,
                evaluation_plan=TrustedEvaluationPlan(),
                memory_paths=memory_paths,
                skill_source_dirs=(),
                skill_selection=NULL_SKILL_SELECTION,
                host_resources=(),
                events=events or (lambda _event: None),
                threads=OsThreads(),
                blocking=ThreadBlockingRunner(),
            )
            env = WorkspaceEnv(project.project, factory, git_disk=disk)
            env.start_host()
            yield env
