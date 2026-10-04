"""Public composition contract for runtime-owned workspace resources."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support.run_execution import run_execution_record

from vs_agent.api import NULL_SKILL_SELECTION, AgentBackend, AgentSpec
from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    RunEnvironmentRecord,
)
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentPaths,
    LocalEnvironment,
    ProjectRunEffects,
    ProjectRunRequest,
    RunEnvironmentPresentation,
    RunEnvironmentRequest,
    RunEnvironmentView,
    TrustedEvaluationPlan,
    WorkspaceResourceFactory,
    WorkspaceRestoreFailed,
    open_project_run_resources,
    open_run_environment_resources,
)
from vs_runtime.api.testing import FakeAgentExecutionEnvironment
from vs_sandbox.api import HostResource, HostResourceAccess, ProjectPathPolicy
from vs_sandbox.api.testing import FakeComputeBackend, FakeSandbox

if TYPE_CHECKING:
    from typing import TextIO

    from vs_project.api import OrchestrationRunManifest
    from vs_runtime.api import OrchestrationResumeDecision
    from vs_runtime.api.infrastructure import RunEnvironmentSession
    from vs_sandbox.api import Sandbox


@dataclass
class _Session:
    sandbox: Sandbox
    view: RunEnvironmentView
    closed: bool = False

    def __enter__(self) -> _Session:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb
        self.close()

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


def _project_request(root: Path) -> ProjectRunRequest:
    return ProjectRunRequest(
        project_root=root,
        run_id="workspace-resource-test",
        display_name="workspace resource test",
        task_name=None,
        existing=False,
        framework_version="1.2.3",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="test-policy", config_version=1, options={}),
    )


def _effects() -> ProjectRunEffects:
    def emit(text: str, writer: TextIO) -> None:
        writer.write(text + "\n")
        writer.flush()

    return ProjectRunEffects(
        git_events=NullGitTrackerEvents(),
        log_emit=emit,
        on_log_ready=lambda _path: None,
    )


def _unexpected_resume(
    _manifest: OrchestrationRunManifest,
) -> OrchestrationResumeDecision:
    raise AssertionError


def test_factory_owns_candidate_lifecycle_and_reports_restore_failure(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = open_project_run_resources(
        _project_request(root), effects=_effects(), resolve_resume=_unexpected_resume
    )
    backend = FakeComputeBackend()
    opened_sessions: list[_Session] = []

    def open_session(_request: RunEnvironmentRequest) -> RunEnvironmentSession:
        session = _Session(
            FakeSandbox(),
            RunEnvironmentView(paths=AgentPaths(), supports_parallel_candidate_evaluation=True),
        )
        opened_sessions.append(session)
        return session

    environment = open_run_environment_resources(
        RunEnvironmentRequest(
            log_dir=project.logger.log_dir,
            workspace=root,
            ref_dir=None,
            backend=backend,
            agent_backend="stub",
            cli_provider="codex",
            run_id="workspace-resource-test",
            framework_root=tmp_path,
            project_path_policy=ProjectPathPolicy(),
        ),
        open_session,
    )
    events: list[WorkspaceRestoreFailed] = []
    factory = WorkspaceResourceFactory(
        project,
        environment,
        evaluation_plan=TrustedEvaluationPlan(),
        memory_paths=("MEMORY.md",),
        skill_source_dirs=(),
        skill_selection=NULL_SKILL_SELECTION,
        host_resources=(),
        events=events.append,
    )
    revision = factory.root.revision
    assert revision is not None

    candidate = factory.create_candidate("candidate-1", revision)
    candidate_path = candidate.path
    assert candidate_path.is_dir()
    assert candidate.revision == revision
    assert factory.supports_parallel_candidates
    assert not factory.root.try_restore("missing-revision", clean=True)
    assert events == [WorkspaceRestoreFailed("missing-revision")]

    candidate.close()
    assert not candidate_path.exists()
    assert opened_sessions[-1].closed
    environment.close()
    project.close()


def test_root_agent_scope_uses_injected_environment_opener(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    project = open_project_run_resources(
        _project_request(root), effects=_effects(), resolve_resume=_unexpected_resume
    )
    backend = FakeComputeBackend()
    environment = open_run_environment_resources(
        RunEnvironmentRequest(
            log_dir=project.logger.log_dir,
            workspace=root,
            ref_dir=None,
            backend=backend,
            agent_backend="stub",
            cli_provider="codex",
            run_id="workspace-resource-test",
            framework_root=tmp_path,
        ),
        lambda _request: _Session(FakeSandbox(), RunEnvironmentView(paths=AgentPaths())),
    )
    opened = FakeAgentExecutionEnvironment(project_path_policy=ProjectPathPolicy())
    configurations: list[AgentExecutionConfiguration] = []

    def open_agent(
        configuration: AgentExecutionConfiguration,
    ) -> FakeAgentExecutionEnvironment:
        configurations.append(configuration)
        return opened

    factory = WorkspaceResourceFactory(
        project,
        environment,
        evaluation_plan=TrustedEvaluationPlan(),
        memory_paths=(),
        skill_source_dirs=(),
        skill_selection=NULL_SKILL_SELECTION,
        host_resources=(),
        events=lambda _event: None,
        root_agent_environment_opener=open_agent,
    )
    configuration = AgentExecutionConfiguration("worker", AgentSpec(backend=AgentBackend.STUB))

    assert factory.root.agent_scope().open_environment(configuration) is opened
    assert configurations == [configuration]
    environment.close()
    project.close()


@pytest.mark.parametrize("revision_source", ["input-baseline", "run-head"])
@pytest.mark.parametrize("agent_objective", ["goal\n", "override\n"])
def test_candidate_environment_uses_the_runs_verified_objective(
    tmp_path: Path, revision_source: str, agent_objective: str
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    with open_project_run_resources(
        replace(_project_request(root), objective="goal\n"),
        effects=_effects(),
        resolve_resume=_unexpected_resume,
    ) as project:
        authored = project.objective_document if agent_objective == "goal\n" else None
        request = RunEnvironmentRequest(
            log_dir=project.logger.log_dir,
            workspace=root,
            ref_dir=None,
            backend=FakeComputeBackend(),
            agent_backend="stub",
            cli_provider=None,
            run_id="workspace-resource-test",
            framework_root=tmp_path,
            objective=agent_objective,
            objective_document=authored,
            git_history_root=project.git.history_root,
        )
        objective_paths: list[str] = []

        def open_session(candidate: RunEnvironmentRequest) -> RunEnvironmentSession:
            session = (
                LocalEnvironment()
                .prepare(candidate)
                .open(RunEnvironmentPresentation(prompt_notes=""))
            )
            objective_paths.append(session.view.paths.objective)
            return session

        with closing(open_run_environment_resources(request, open_session)) as environment:
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
            revision = (
                project.git.trusted_input_baseline
                if revision_source == "input-baseline"
                else project.git.current_sha()
            )
            assert revision is not None
            candidate = factory.create_candidate("candidate-1", revision)
            try:
                assert Path(objective_paths[-1]).read_text() == agent_objective
                if authored is not None:
                    assert objective_paths[-1] == str(authored)
                configuration = AgentExecutionConfiguration(
                    "worker", AgentSpec(backend=AgentBackend.STUB)
                )
                with closing(candidate.agent_scope().open_environment(configuration)) as scoped:
                    assert (
                        HostResource(
                            path=Path(objective_paths[-1]), access=HostResourceAccess.READ_ONLY
                        )
                        in scoped.host_resources
                    )
            finally:
                candidate.close()
