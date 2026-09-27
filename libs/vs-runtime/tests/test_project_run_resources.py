"""Public contracts for lower-owned project-run resources."""

from __future__ import annotations

import sys
from dataclasses import replace
from subprocess import CalledProcessError
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel, ConfigDict
from tests.support.run_execution import run_execution_record

from vs_project.api import (
    NullGitTrackerEvents,
    OrchestrationDescriptor,
    Project,
    RunEnvironmentRecord,
)
from vs_runtime.api import OrchestrationResumeDecision
from vs_runtime.api.infrastructure import (
    ProjectMaterializer,
    ProjectRunDirtyResumeError,
    ProjectRunEffects,
    ProjectRunMismatchError,
    ProjectRunMismatchKind,
    ProjectRunRequest,
    ProjectStateDeclaration,
    SDKRoots,
    open_project_run_resources,
)
from vs_runtime.api.testing import FakeProjectMaterializationEffects

if TYPE_CHECKING:
    from pathlib import Path
    from typing import TextIO

    from vs_project.api import OrchestrationRunManifest

_RUN_ID = "project-run-test"


class _PolicyState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    completed_rounds: tuple[int, ...] = ()


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


def _descriptor(*, rounds: int = 1) -> OrchestrationDescriptor:
    return OrchestrationDescriptor(
        id="test-policy",
        config_version=1,
        options={"rounds": rounds},
    )


def _request(
    root: Path,
    *,
    existing: bool = False,
    task_name: str | None = None,
    descriptor: OrchestrationDescriptor | None = None,
) -> ProjectRunRequest:
    return ProjectRunRequest(
        project_root=root,
        run_id=_RUN_ID,
        display_name="project run test",
        task_name=task_name,
        existing=existing,
        framework_version="1.2.3",
        run_environment=RunEnvironmentRecord(name="local"),
        execution=run_execution_record(),
        orchestration=descriptor or _descriptor(),
        state=ProjectStateDeclaration("policy", _PolicyState),
    )


def _materializer(root: Path) -> ProjectMaterializer:
    return ProjectMaterializer(
        root,
        effects=FakeProjectMaterializationEffects(),
        log=lambda _message: None,
        sdk_roots=SDKRoots(checkout=root.parent / "sdk", packaged=root.parent / "sdk"),
        excluded_dirs=(),
    )


def _effects(events: list[str]) -> ProjectRunEffects:
    def emit(text: str, writer: TextIO) -> None:
        events.append(f"log:{text}")
        writer.write(text + "\n")
        writer.flush()

    def on_log_ready(log_dir: Path) -> None:
        events.append(f"ready:{log_dir.name}")

    return ProjectRunEffects(
        git_events=NullGitTrackerEvents(),
        log_emit=emit,
        on_log_ready=on_log_ready,
    )


def _unexpected_resume(_manifest: OrchestrationRunManifest) -> OrchestrationResumeDecision:
    raise AssertionError


def _write_project(root: Path) -> None:
    root.mkdir()
    (root / "candidate.py").write_text("VALUE = 1\n", encoding="utf-8")


def test_fresh_run_owns_manifest_git_logger_and_state(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)
    events: list[str] = []
    original_stderr = sys.stderr

    resources = open_project_run_resources(
        _request(root),
        effects=_effects(events),
        buffered_logs=("before logger",),
        resolve_resume=_unexpected_resume,
    )
    manifest = resources.project.state.load_run(_RUN_ID)

    assert manifest.vibesys_version == "1.2.3"
    assert manifest.orchestration == _descriptor()
    assert resources.git.project_branch == manifest.branch
    assert resources.state.run_id == _RUN_ID
    assert resources.round_transaction_coordinator is not None
    assert events == ["ready:logs", "log:before logger"]
    assert sys.stderr is not original_stderr

    resources.close()
    resources.close()
    assert resources.logger.writer.closed
    assert sys.stderr is original_stderr


def test_project_run_commits_the_effective_objective(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)

    with open_project_run_resources(
        replace(_request(root), objective="Keep latency below 10 ms.\n"),
        effects=_effects([]),
        resolve_resume=_unexpected_resume,
    ) as resources:
        document = resources.objective_document
        assert document is not None
        assert document.read_text() == "Keep latency below 10 ms.\n"
        assert resources.git.pending_changes() == []


def test_unready_project_run_discards_its_provisional_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)
    resources = open_project_run_resources(
        replace(_request(root), provisional_project=_materializer(root)),
        effects=_effects([]),
        resolve_resume=_unexpected_resume,
    )

    resources.close()

    assert not root.exists()


def test_ready_project_run_preserves_its_provisional_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)
    resources = open_project_run_resources(
        replace(_request(root), provisional_project=_materializer(root)),
        effects=_effects([]),
        resolve_resume=_unexpected_resume,
    )

    resources.mark_ready()
    resources.mark_ready()
    resources.close()

    assert root.is_dir()


def test_candidate_resources_own_linked_worktree_git_and_logger(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)
    resources = open_project_run_resources(
        _request(root), effects=_effects([]), resolve_resume=_unexpected_resume
    )
    revision = resources.git.current_sha()
    assert revision is not None

    candidate = resources.open_candidate("candidate-1", revision)
    candidate_path = candidate.project_root
    assert candidate_path.is_dir()
    assert candidate.git.current_sha() == revision
    assert candidate.git.trusted_input_baseline == resources.git.trusted_input_baseline
    candidate.logger.lprint("candidate message")
    assert "candidate message" in candidate.logger.path.read_text(encoding="utf-8")

    candidate.close()
    candidate.close()
    assert not candidate_path.exists()
    assert candidate.logger.writer.closed
    resources.close()


def test_candidate_construction_failure_removes_partial_worktree(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)
    with open_project_run_resources(
        _request(root), effects=_effects([]), resolve_resume=_unexpected_resume
    ) as resources:
        candidate_path = resources.project.state.candidate_worktree_directory(
            _RUN_ID, "candidate-1"
        )

        with pytest.raises(CalledProcessError):
            resources.open_candidate("candidate-1", "not-a-revision")

        assert not candidate_path.exists()


def test_resume_applies_policy_decision_and_restores_current_run(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)
    effects = _effects([])
    with open_project_run_resources(
        _request(root), effects=effects, resolve_resume=_unexpected_resume
    ):
        pass

    requested = _descriptor(rounds=2)

    def resume(recorded: OrchestrationRunManifest) -> OrchestrationResumeDecision:
        assert recorded.orchestration == _descriptor()
        return OrchestrationResumeDecision(descriptor=requested)

    with open_project_run_resources(
        _request(root, existing=True, descriptor=requested),
        effects=effects,
        resolve_resume=resume,
    ) as resources:
        assert resources.project.state.load_run(_RUN_ID).orchestration == requested
        assert resources.project.state.current_run_id() == _RUN_ID


def test_resume_recovers_unfinished_typed_state_transaction(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)
    events: list[str] = []
    effects = _effects(events)
    resources = open_project_run_resources(
        _request(root), effects=effects, resolve_resume=_unexpected_resume
    )
    coordinator = resources.round_transaction_coordinator
    assert coordinator is not None
    coordinator.begin(
        1,
        writes={"state.json": _PolicyState(completed_rounds=(1,))},
        candidate=False,
    )
    resources.close()

    with open_project_run_resources(
        _request(root, existing=True),
        effects=effects,
        resolve_resume=lambda _recorded: OrchestrationResumeDecision(descriptor=None),
    ) as resumed:
        state = resumed.project.state.portable_namespace(_RUN_ID, "policy").load(
            "state.json", _PolicyState
        )
        assert state == _PolicyState(completed_rounds=(1,))

    assert "log:[project] recovered round transaction: committed" in events


def test_resume_reports_policy_neutral_task_mismatch_and_cleans_logger(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)
    effects = _effects([])
    with open_project_run_resources(
        _request(root), effects=effects, resolve_resume=_unexpected_resume
    ):
        pass
    original_stderr = sys.stderr

    with pytest.raises(ProjectRunMismatchError) as raised:
        open_project_run_resources(
            _request(root, existing=True, task_name="different-task"),
            effects=effects,
            resolve_resume=lambda _recorded: OrchestrationResumeDecision(descriptor=None),
        )

    assert raised.value.kind is ProjectRunMismatchKind.TASK
    assert raised.value.recorded is None
    assert raised.value.actual == "different-task"
    assert sys.stderr is original_stderr


def test_clean_resume_requirement_reports_pending_paths_and_cleans_logger(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    effects = _effects([])
    with open_project_run_resources(
        _request(root), effects=effects, resolve_resume=_unexpected_resume
    ):
        pass
    (root / "candidate.py").write_text("VALUE = 2\n", encoding="utf-8")
    original_stderr = sys.stderr

    with pytest.raises(ProjectRunDirtyResumeError) as raised:
        open_project_run_resources(
            _request(root, existing=True, descriptor=_descriptor(rounds=2)),
            effects=effects,
            resolve_resume=lambda _recorded: OrchestrationResumeDecision(
                descriptor=_descriptor(rounds=2),
                requires_clean_workspace=True,
            ),
        )

    assert raised.value.pending == ("candidate.py",)
    assert sys.stderr is original_stderr


def test_resume_policy_failure_preserves_the_primary_error_and_cleans_logger(
    tmp_path: Path,
) -> None:
    root = tmp_path / "project"
    _write_project(root)
    effects = _effects([])
    with open_project_run_resources(
        _request(root), effects=effects, resolve_resume=_unexpected_resume
    ):
        pass
    original_stderr = sys.stderr
    failure = RuntimeError("resume policy failed")

    def fail(_recorded: OrchestrationRunManifest) -> OrchestrationResumeDecision:
        raise failure

    with pytest.raises(RuntimeError, match="resume policy failed") as raised:
        open_project_run_resources(
            _request(root, existing=True),
            effects=effects,
            resolve_resume=fail,
        )

    assert raised.value is failure
    assert sys.stderr is original_stderr


def test_project_run_request_uses_canonical_project_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    _write_project(root)

    with open_project_run_resources(
        _request(root), effects=_effects([]), resolve_resume=_unexpected_resume
    ) as resources:
        assert resources.project.root == Project.open(root).root
        assert resources.git.history_root == resources.project.root
