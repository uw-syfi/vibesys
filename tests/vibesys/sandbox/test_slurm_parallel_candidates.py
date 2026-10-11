"""Each parallel candidate of a ``slurm`` run with agent GPU commands has a broker of its own.

The dynamic loop opens one container session per candidate, and every session
starts a host broker that runs the agent's GPU commands. The broker must serve
its own candidate's worktree and no other, or one candidate's agent could write
into another candidate's files with a GPU job. The runs below open candidates
through the run's workspace API over a real ``SlurmEnvironment``, a fake Docker
daemon, and a fake ``srun`` program, and drive the agent's launcher from each
candidate's container.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest
from tests.support.workspace_world import open_workspace_env

from vibesys.run.environment import open_run_environment
from vs_runtime.api.infrastructure import SlurmEnvironment
from vs_runtime.api.testing import DaemonBackend, daemon_docker_config, daemon_engine
from vs_sandbox.api.slurm import load_slurm_operator_settings

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime.api.infrastructure import (
        RunEnvironmentRequest,
        RunEnvironmentSession,
        RuntimeWorkspaces,
    )
    from vs_sandbox.api import CommandResult

_CONFIG = """[slurm]
name = "test-cluster"
remote_workspace_root = "/shared/vibesys"

[slurm.transport]
kind = "local"
shell_command = ["{python}", "{srun}"]

[vibesys.agent_gpu]
partitions = ["main"]
max_gpus = 4
max_time_minutes = 90
"""
# Stands in for the Slurm shell: runs what follows ``--`` where it was started.
_FAKE_SRUN = """\
import os, shlex, sys
args = shlex.split(sys.argv[1])
os.execvp(args[args.index("--") + 1], args[args.index("--") + 1 :])
"""
_LAUNCH = '"$VIBESYS_GPU" --gpus 1 -- '


class _PassThroughConfinement:
    def wrap(self, workspace: Path, argv: object) -> list[str]:
        del workspace
        return list(argv)  # ty: ignore[invalid-argument-type]


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


def _environment(tmp_path: Path, backend: DaemonBackend) -> SlurmEnvironment:
    srun = tmp_path / "fake_srun.py"
    srun.write_text(_FAKE_SRUN, encoding="utf-8")
    config = tmp_path / "slurm.toml"
    config.write_text(_CONFIG.format(python=sys.executable, srun=srun), encoding="utf-8")
    return SlurmEnvironment(
        load_slurm_operator_settings(config),
        docker=daemon_docker_config(backend.engine),
        job_confinement=_PassThroughConfinement(),
    )


def _run(tmp_path: Path, exercise: object) -> None:
    """Open a run whose candidates are Slurm sessions, and await *exercise* with its workspaces."""
    backend = DaemonBackend(daemon_engine(tmp_path))
    environment = _environment(tmp_path, backend)

    def open_session(request: RunEnvironmentRequest) -> RunEnvironmentSession:
        return open_run_environment(environment, request)

    world = tmp_path / "world"
    world.mkdir()
    with open_workspace_env(
        world,
        backend=backend,
        open_session=open_session,
        owns_candidate_worktrees=True,
    ) as env:
        try:
            asyncio.run(exercise(env.hosts[0]))  # ty: ignore[call-non-callable]
        finally:
            for host in reversed(env.hosts):
                with suppress(Exception):
                    asyncio.run(host.close())


def _in(workspaces: RuntimeWorkspaces, path: Path, command: str) -> CommandResult:
    sandbox = workspaces.agent_sandbox_at(path)
    assert sandbox is not None
    return sandbox.execute(command)


def test_a_candidates_gpu_command_runs_in_its_own_worktree(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        first = await workspaces.create_candidate()
        second = await workspaces.create_candidate()

        for candidate in (first, second):
            ran = _in(workspaces, candidate.path, f"{_LAUNCH}pwd")
            assert ran.exit_code == 0, ran.output
            assert str(candidate.path) in ran.output.splitlines()

    _run(tmp_path, exercise)


def test_a_candidates_gpu_command_is_refused_in_another_candidates_worktree(
    tmp_path: Path,
) -> None:
    """Regression: every candidate's broker accepted every run-owned worktree."""

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        first = await workspaces.create_candidate()
        second = await workspaces.create_candidate()

        for mine, other in ((first, second), (second, first)):
            refused = _in(workspaces, mine.path, f"cd {other.path} && {_LAUNCH}touch intruder")

            assert refused.exit_code != 0
            assert "inside the run's workspace" in refused.output
            assert not (other.path / "intruder").exists()

    _run(tmp_path, exercise)


def test_every_candidate_session_has_its_own_socket_and_token(tmp_path: Path) -> None:
    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        opened = [await workspaces.create_candidate() for _ in range(3)]

        seen = {
            tuple(
                _in(
                    workspaces,
                    candidate.path,
                    'echo "$VIBESYS_COMMAND_BROKER_SOCKET $VIBESYS_COMMAND_BROKER_TOKEN"',
                )
                .output.strip()
                .split()
            )
            for candidate in opened
        }

        assert len(seen) == len(opened)
        assert all(len(pair) == 2 for pair in seen)
        assert len({socket for socket, _ in seen}) == len(opened)
        assert len({token for _, token in seen}) == len(opened)

    _run(tmp_path, exercise)
