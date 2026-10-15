"""A real agent container can use Git in a candidate's linked worktree without altering the project.

Skipped unless ``VIBESYS_E2E_DOCKER=1`` and ``docker`` is on PATH, so an ordinary
``pytest`` run needs neither Docker nor the agent image:

```bash
VIBESYS_E2E_DOCKER=1 uv run pytest tests/e2e/test_candidate_git_in_container_e2e.py -q -s
```

The workspace is a linked Git worktree whose ``.git`` file names its metadata by a
host path. Before the run environment mounted that metadata, every ``git`` command
in the container failed with "not a git repository" (a judge could not run
``git diff``).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vibesys.run.environment import open_run_environment
from vs_runtime.api.infrastructure import (
    EnvironmentSystem,
    RunEnvironmentRequest,
    RunEnvironmentSpec,
    build_run_environment,
)
from vs_sandbox.api import ComputeBackend, ProjectPathPolicy, SubprocessDockerCli
from vs_sandbox.local_compute_backend import LocalBackend
from vs_sim.api import (
    MonotonicClock,
    OsProcesses,
    OsThreads,
    SubprocessPipedProcessLauncher,
    UnixNetwork,
)
from vs_sim.api.testing import NO_PROCESSES

if TYPE_CHECKING:
    from collections.abc import Iterator

    from vs_runtime.api.infrastructure import RunEnvironmentSession

pytestmark = pytest.mark.e2e

_ENABLE_ENV = "VIBESYS_E2E_DOCKER"
_BASE_IMAGE = os.environ.get("VIBESYS_E2E_DOCKER_BASE_IMAGE", "python:3.12-bookworm")
_IDENTITY = ("-c", "user.name=t", "-c", "user.email=t@example.invalid")


def _git(*args: str, cwd: Path) -> str:
    done = run_test_command(
        ["git", *_IDENTITY, *args], cwd=cwd, check=True, capture_output=True, text=True
    )
    return done.stdout


@pytest.fixture
def candidate(tmp_path: Path) -> Iterator[tuple[RunEnvironmentSession, Path, Path]]:
    """Yield a started session over a linked worktree, the worktree, and a sibling worktree."""
    if os.environ.get(_ENABLE_ENV) != "1" or shutil.which("docker") is None:
        pytest.skip(f"set {_ENABLE_ENV}=1 with docker on PATH to run against a real daemon")
    project = tmp_path / "project"
    project.mkdir()
    _git("init", "-q", cwd=project)
    (project / "engine.py").write_text("print(1)\n")
    _git("add", "engine.py", cwd=project)
    _git("commit", "-q", "-m", "base", cwd=project)
    workspace = tmp_path / "run" / "workspace-a"
    sibling = tmp_path / "run" / "workspace-b"
    _git("worktree", "add", "-q", "--detach", str(workspace), cwd=project)
    _git("worktree", "add", "-q", "--detach", str(sibling), cwd=project)
    (workspace / "engine.py").write_text("print(2)\n")
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    request = RunEnvironmentRequest(
        log_dir=log_dir,
        workspace=workspace,
        ref_dir=None,
        backend=LocalBackend(
            ComputeBackend.CPU,
            log_dir,
            image=_BASE_IMAGE,
            docker=SubprocessDockerCli(
                processes=OsProcesses(), launcher=SubprocessPipedProcessLauncher()
            ),
            threads=OsThreads(),
            launcher=SubprocessPipedProcessLauncher(),
        ),
        agent_backend="stub",
        cli_provider=None,
        run_id="candidate-git-e2e",
        framework_root=tmp_path,
        project_path_policy=ProjectPathPolicy(),
        git_history_root=project,
        clock=MonotonicClock(),
        system=_real_system(),
    )
    session = open_run_environment(build_run_environment(RunEnvironmentSpec("docker")), request)
    try:
        yield session, workspace, sibling
    finally:
        session.close()


def test_git_works_in_the_candidate_worktree(
    candidate: tuple[RunEnvironmentSession, Path, Path],
) -> None:
    session, _workspace, _sibling = candidate

    inside = session.sandbox.execute("git rev-parse --is-inside-work-tree")
    status = session.sandbox.execute("git status --porcelain")
    diff = session.sandbox.execute("git diff --name-only")

    assert inside.exit_code == 0, inside.output
    assert inside.output.strip() == "true"
    assert status.exit_code == 0, status.output
    assert "engine.py" in status.output
    assert diff.exit_code == 0, diff.output
    assert diff.output.split() == ["engine.py"]


def test_git_in_the_container_cannot_alter_refs_or_other_worktrees(
    candidate: tuple[RunEnvironmentSession, Path, Path],
) -> None:
    session, workspace, sibling = candidate
    project = Path(_git("rev-parse", "--git-common-dir", cwd=workspace).strip()).resolve().parent
    refs_before = _git("for-each-ref", cwd=project)
    sibling_head = _git("rev-parse", "HEAD", cwd=sibling)

    attempts = [
        "git update-ref refs/heads/escaped HEAD",
        "git -c user.name=t -c user.email=t@example.invalid commit -qam escaped",
        f"git -C {sibling} checkout -q --detach HEAD~0",
        f"printf x > {sibling}/intruder",
    ]
    results = [session.sandbox.execute(command) for command in attempts]

    assert all(result.exit_code != 0 for result in results), [r.output for r in results]
    assert _git("for-each-ref", cwd=project) == refs_before
    assert _git("rev-parse", "HEAD", cwd=sibling) == sibling_head
    assert not (sibling / "intruder").exists()


def _real_system() -> EnvironmentSystem:
    """The host's own threads, sockets and processes; this test runs no Slurm program."""
    return EnvironmentSystem(
        threads=OsThreads(),
        network=UnixNetwork(),
        processes=OsProcesses(),
        launcher=SubprocessPipedProcessLauncher(),
        slurm_processes=NO_PROCESSES,
    )
