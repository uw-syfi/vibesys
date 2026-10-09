"""Canonical confinement remains valid in workspaces without ignored run state."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys.run.environment import open_run_environment
from vibesys.run.project_policy import build_project_path_policy
from vs_project.api import Project
from vs_runtime.api.infrastructure import (
    RunEnvironmentRequest,
    RunEnvironmentSpec,
    build_run_environment,
)
from vs_sandbox.api.testing import FakeComputeBackend

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.parametrize("candidate", [False, True])
def test_run_environment_prepares_hidden_state_before_opening(
    tmp_path: Path, *, candidate: bool
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    project = Project.open(canonical)
    project.state.candidate_worktrees_directory("run").mkdir(parents=True)
    policy = build_project_path_policy(canonical, evaluator_source=None)
    workspace = tmp_path / "candidate" if candidate else canonical
    workspace.mkdir(exist_ok=True)
    if candidate:
        with pytest.raises(ValueError, match="hidden project path does not exist"):
            policy.resolve(workspace)
    request = RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=FakeComputeBackend(),
        agent_backend="stub",
        cli_provider=None,
        run_id="run",
        framework_root=tmp_path,
        project_path_policy=policy,
    )

    with open_run_environment(build_run_environment(RunEnvironmentSpec("local")), request):
        policy.resolve(workspace)
        assert Project.open(workspace).state.candidate_worktrees_directory("run").is_dir()
        assert build_project_path_policy(canonical, evaluator_source=None) == policy
