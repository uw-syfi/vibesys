"""Canonical confinement remains valid in workspaces without ignored run state."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import run_test_command

from vibesys.run.environment import open_run_environment
from vibesys.run.project_policy import build_project_path_policy
from vs_project.api import Project
from vs_runtime.api.infrastructure import RunEnvironmentRequest
from vs_runtime.api.testing import idle_environment_system, unconfined_host_environment
from vs_sandbox.api.docker_workspace import docker_project_path_resources
from vs_sandbox.api.testing import FakeComputeBackend
from vs_sim.api.testing import ManualClock, SimThreads


@pytest.mark.parametrize("candidate", [False, True])
def test_run_environment_prepares_hidden_state_before_opening(
    tmp_path: Path, *, candidate: bool
) -> None:
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    project = Project.open(canonical)
    project.state.candidate_worktrees_directory("run").mkdir(parents=True)
    (canonical / ".vibesys/state/runs").mkdir(parents=True)
    policy = build_project_path_policy(canonical, evaluator_source=None)
    assert {Path(".vibesys/state/local"), Path(".vibesys/state/runs")} <= set(policy.hidden_paths)
    workspace = tmp_path / "candidate" if candidate else canonical
    workspace.mkdir(exist_ok=True)
    if candidate:
        with pytest.raises(ValueError, match="hidden project path does not exist"):
            policy.resolve(workspace)
    request = RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=workspace,
        ref_dir=None,
        backend=FakeComputeBackend(threads=SimThreads()),
        agent_backend="stub",
        cli_provider=None,
        run_id="run",
        framework_root=tmp_path,
        project_path_policy=policy,
        clock=ManualClock(),
        system=idle_environment_system(),
    )

    with open_run_environment(unconfined_host_environment(), request):
        policy.resolve(workspace)
        assert Project.open(workspace).state.candidate_worktrees_directory("run").is_dir()
        assert build_project_path_policy(canonical, evaluator_source=None) == policy


def test_uncommitted_secret_files_are_hidden_in_a_linked_worktree(tmp_path: Path) -> None:
    """A worktree omits ignored ``agent.toml`` and ``.env*``; confinement still masks them."""
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    identity = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")
    for args in (
        ("init", "-q"),
        ("commit", "-q", "--allow-empty", "-m", "base"),
    ):
        run_test_command(["git", *identity, *args], cwd=canonical, check=True, capture_output=True)
    operator_config = "[model]\nname = 'operator'\n"
    (canonical / "agent.toml").write_text(operator_config)
    (canonical / ".env.local").write_text("NAME=operator\n")
    worktree = tmp_path / "worktree"
    run_test_command(
        ["git", *identity, "worktree", "add", "-q", "--detach", str(worktree)],
        cwd=canonical,
        check=True,
        capture_output=True,
    )
    policy = build_project_path_policy(canonical, evaluator_source=None)
    assert {Path("agent.toml"), Path(".env.local")} <= set(policy.hidden_paths)
    assert not (worktree / "agent.toml").exists()
    with pytest.raises(ValueError, match="hidden project path does not exist"):
        policy.resolve(worktree)

    request = RunEnvironmentRequest(
        log_dir=tmp_path / "logs",
        workspace=worktree,
        ref_dir=None,
        backend=FakeComputeBackend(threads=SimThreads()),
        agent_backend="stub",
        cli_provider=None,
        run_id="run",
        framework_root=tmp_path,
        project_path_policy=policy,
        clock=ManualClock(),
        system=idle_environment_system(),
    )
    with open_run_environment(unconfined_host_environment(), request):
        policy.resolve(worktree)
        resources = docker_project_path_resources(policy, worktree, mask_root=tmp_path / "masks")

    masked = {
        resource.agent_path: resource.path
        for resource in resources
        if resource.agent_path in {"/workspace/agent.toml", "/workspace/.env.local"}
    }
    assert masked["/workspace/agent.toml"].is_file()
    assert (worktree / "agent.toml").read_text() == ""
    assert (worktree / ".env.local").read_text() == ""
    assert (canonical / "agent.toml").read_text() == operator_config
    assert (canonical / ".env.local").read_text() == "NAME=operator\n"
