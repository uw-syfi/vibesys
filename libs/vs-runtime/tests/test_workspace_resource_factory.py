"""Public composition contract for runtime-owned workspace resources."""

from __future__ import annotations

import os
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vs_agent.api import NULL_SKILL_SELECTION, AgentBackend, AgentSpec
from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    RunEnvironmentRecord,
)
from vs_project.api.testing import FakeGitRepositories, run_execution_record
from vs_runtime.api.core import BaselineEntryKind
from vs_runtime.api.infrastructure import (
    AgentExecutionConfiguration,
    AgentPaths,
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
from vs_runtime.api.testing import (
    FakeAgentExecutionEnvironment,
    idle_environment_system,
    unconfined_host_environment,
)
from vs_sandbox.api import HostResource, HostResourceAccess, ProjectPathPolicy
from vs_sandbox.api.testing import FakeCommandRunner, FakeComputeBackend
from vs_sim.api.testing import InlineBlockingRunner, ManualClock, SimThreads

if TYPE_CHECKING:
    from typing import TextIO

    from vs_project.api import OrchestrationRunManifest
    from vs_runtime.api import OrchestrationResumeDecision
    from vs_runtime.api.infrastructure import RunEnvironmentSession, WorkspaceResourceEvent
    from vs_sandbox.api import CommandRunner


@dataclass
class _Session:
    sandbox: CommandRunner
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
        run_environment=RunEnvironmentRecord(name="docker"),
        execution=run_execution_record(),
        orchestration=OrchestrationDescriptor(id="test-policy", config_version=1, options={}),
    )


# In-memory Git: these cases test run resources, not the Git CLI. Directories are unique per test.
_DISK = FakeGitRepositories()


def _effects() -> ProjectRunEffects:
    def emit(text: str, writer: TextIO) -> None:
        writer.write(text + "\n")
        writer.flush()

    return ProjectRunEffects(
        git_events=NullGitTrackerEvents(),
        git_repository=_DISK.repository,
        log_emit=emit,
        on_log_ready=lambda _path: None,
        clock=ManualClock(),
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
    backend = FakeComputeBackend(threads=SimThreads())
    opened_sessions: list[_Session] = []

    def open_session(_request: RunEnvironmentRequest) -> RunEnvironmentSession:
        session = _Session(
            FakeCommandRunner(),
            RunEnvironmentView(paths=AgentPaths(), cli_sandboxed=True),
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
            clock=ManualClock(),
            system=idle_environment_system(),
        ),
        open_session,
        clock=ManualClock(),
    )
    events: list[WorkspaceResourceEvent] = []
    factory = WorkspaceResourceFactory(
        project,
        environment,
        evaluation_plan=TrustedEvaluationPlan(),
        memory_paths=("MEMORY.md",),
        skill_source_dirs=(),
        skill_selection=NULL_SKILL_SELECTION,
        host_resources=(),
        events=events.append,
        threads=SimThreads(),
        blocking=InlineBlockingRunner(),
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
    backend = FakeComputeBackend(threads=SimThreads())
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
            clock=ManualClock(),
            system=idle_environment_system(),
        ),
        lambda _request: _Session(FakeCommandRunner(), RunEnvironmentView(paths=AgentPaths())),
        clock=ManualClock(),
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
        threads=SimThreads(),
        blocking=InlineBlockingRunner(),
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
            backend=FakeComputeBackend(threads=SimThreads()),
            agent_backend="stub",
            cli_provider=None,
            run_id="workspace-resource-test",
            framework_root=tmp_path,
            objective=agent_objective,
            objective_document=authored,
            git_history_root=project.git.history_root,
            clock=ManualClock(),
            system=idle_environment_system(),
        )
        objective_paths: list[str] = []

        def open_session(candidate: RunEnvironmentRequest) -> RunEnvironmentSession:
            session = (
                unconfined_host_environment()
                .prepare(candidate)
                .open(RunEnvironmentPresentation(prompt_notes=""))
            )
            objective_paths.append(session.view.paths.objective)
            return session

        with closing(
            open_run_environment_resources(request, open_session, clock=ManualClock())
        ) as environment:
            factory = WorkspaceResourceFactory(
                project,
                environment,
                evaluation_plan=TrustedEvaluationPlan(),
                memory_paths=(),
                skill_source_dirs=(),
                skill_selection=NULL_SKILL_SELECTION,
                host_resources=(),
                events=lambda _event: None,
                threads=SimThreads(),
                blocking=InlineBlockingRunner(),
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


def test_a_baseline_never_fingerprints_a_file_outside_the_workspace(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "weights.bin").write_bytes(b"w" * 3_000_000)
    (root / "cache").mkdir()
    outside = tmp_path / "host-secret"
    outside.write_text("secret", encoding="utf-8")
    (root / "planted").symlink_to(outside)
    project = open_project_run_resources(
        _project_request(root), effects=_effects(), resolve_resume=_unexpected_resume
    )
    environment = open_run_environment_resources(
        RunEnvironmentRequest(
            log_dir=project.logger.log_dir,
            workspace=root,
            ref_dir=None,
            backend=FakeComputeBackend(threads=SimThreads()),
            agent_backend="stub",
            cli_provider="codex",
            run_id="workspace-resource-test",
            framework_root=tmp_path,
            clock=ManualClock(),
            system=idle_environment_system(),
        ),
        lambda _request: _Session(FakeCommandRunner(), RunEnvironmentView(paths=AgentPaths())),
        clock=ManualClock(),
    )
    factory = WorkspaceResourceFactory(
        project,
        environment,
        evaluation_plan=TrustedEvaluationPlan(),
        memory_paths=(),
        skill_source_dirs=(),
        skill_selection=NULL_SKILL_SELECTION,
        host_resources=(),
        events=lambda _event: None,
        threads=SimThreads(),
        blocking=InlineBlockingRunner(),
    )

    os.mkfifo(root / "pipe")
    entries = factory.root.baseline_entries(("weights.bin", "planted", "cache/", "pipe"))

    kinds = {entry.path: entry.kind for entry in entries}
    assert kinds == {
        "weights.bin": BaselineEntryKind.FILE,
        "planted": BaselineEntryKind.UNFINGERPRINTED,
        "cache/": BaselineEntryKind.DIRECTORY,
        "pipe": BaselineEntryKind.UNFINGERPRINTED,
    }
    # Nothing changed yet: a path that could not be fingerprinted is not "removed".
    assert factory.root.baseline_drift(entries).is_empty
    (root / "pipe").unlink()
    assert factory.root.baseline_drift(entries).removed == ("pipe",)
    (root / "weights.bin").unlink()
    (root / "weights.bin").symlink_to(outside)
    assert "weights.bin" in factory.root.baseline_drift(entries).removed
    environment.close()
    project.close()
