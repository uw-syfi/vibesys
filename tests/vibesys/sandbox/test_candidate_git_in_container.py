"""A candidate's container can use Git in its worktree and cannot reach other candidates through it.

A candidate workspace is a linked Git worktree: its ``.git`` is a file naming the
worktree's own metadata directory inside the project repository by an absolute
host path. A container that mounts only the workspace cannot follow that path, so
every ``git`` command in it fails with "not a git repository" (seen in a live run
when a judge ran ``git diff``). The runs below open candidates through the run's
workspace API over a fake Docker daemon and walk Git's lookup of the worktree's
repository through the mounts the daemon recorded, the way the container does.
"""

from __future__ import annotations

import asyncio
import posixpath
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command
from tests.support.workspace_world import open_workspace_env

from vibesys.run.environment import open_run_environment
from vs_project.api.testing import GitKind
from vs_runtime.api.infrastructure import DockerEnvironment, SlurmEnvironment
from vs_runtime.api.testing import (
    DaemonBackend,
    daemon_docker_config,
    daemon_engine,
    idle_environment_system,
)
from vs_sandbox.api.slurm import load_slurm_operator_settings
from vs_sim.api.testing import FakePipedProcessLauncher, SimThreads
from vs_slurm.api import FakeConnector

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from vs_runtime.api.infrastructure import (
        RunEnvironment,
        RunEnvironmentRequest,
        RunEnvironmentSession,
        RuntimeWorkspaces,
    )
    from vs_sandbox.api.testing import FakeContainer, FakeDockerEngine

_SLURM_CONFIG = """[slurm]
name = "test-cluster"
remote_workspace_root = "{remote_workspace_root}"

[slurm.transport]
kind = "local"
shell_command = ["fake-connector", "shell"]
rsync_command = ["fake-connector", "rsync"]
"""

# What Git reads from the shared repository directory for ``status``, ``diff`` and ``log``.
_SHARED_ENTRIES = ("objects", "refs", "HEAD")


@pytest.fixture(autouse=True)
def isolated_project_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VIBESYS_STATE_HOME", str(tmp_path / "operator-state"))


def _docker(_tmp_path: Path, engine: FakeDockerEngine) -> RunEnvironment:
    return DockerEnvironment(daemon_docker_config(engine))


def _slurm(tmp_path: Path, engine: FakeDockerEngine) -> RunEnvironment:
    remote = tmp_path / "remote"
    remote.mkdir()
    config = tmp_path / "slurm.toml"
    config.write_text(_SLURM_CONFIG.format(remote_workspace_root=remote), encoding="utf-8")
    return SlurmEnvironment(
        load_slurm_operator_settings(config), docker=daemon_docker_config(engine)
    )


class _ContainerView:
    """What a container sees of the host: the paths its bind mounts expose."""

    def __init__(self, container: FakeContainer) -> None:
        self._container = container

    def resolve(self, path: str) -> tuple[Path, bool] | None:
        """Return the host file behind a container path and whether it is read-only, if mounted."""
        mounts = self._container.mounts
        best = max(
            (prefix for prefix in mounts if path == prefix or path.startswith(prefix + "/")),
            key=len,
            default=None,
        )
        if best is None:
            return None
        host = mounts[best] / path.removeprefix(best).lstrip("/")
        return host, best in self._container.read_only_mounts

    def host_of_workspace(self, workspace: Path) -> str | None:
        """Return where the container mounts *workspace*, if it does."""
        return next((at for at, host in self._container.mounts.items() if host == workspace), None)

    def writable_hosts(self) -> list[Path]:
        """Every host path the container may modify."""
        return [
            host
            for at, host in self._container.mounts.items()
            if at not in self._container.read_only_mounts
        ]

    def exposes_at_its_host_path(self, host: Path) -> bool:
        """Whether a mount makes *host*, or something below it, visible at its own host path.

        The read-only ``/opt/vibesys-history`` mount of the whole project is a separate,
        deliberate exposure under another path and does not count here.
        """
        return any(
            str(mounted) == at and (mounted.is_relative_to(host) or host.is_relative_to(mounted))
            for at, mounted in self._container.mounts.items()
        )

    def mounted_host_files(self) -> list[Path]:
        """Every regular file any bind mount makes visible inside the container."""
        return [host for host in self._container.mounts.values() if host.is_file()]


def _git_lookup_problems(view: _ContainerView, workspace: Path) -> list[str]:
    """Walk Git's lookup of *workspace*'s repository through the container's mounts."""
    at = view.host_of_workspace(workspace)
    if at is None:
        return ["the container does not mount the workspace"]
    pointer = (workspace / ".git").read_text(encoding="utf-8").strip()
    gitdir = pointer.removeprefix("gitdir:").strip()
    found = view.resolve(gitdir)
    if found is None or not found[0].is_dir():
        return [f"the worktree's gitdir is not visible: {gitdir}"]
    problems = []
    host_gitdir, read_only = found
    if not read_only:
        problems.append("the worktree's gitdir is writable")
    common = posixpath.normpath(
        posixpath.join(gitdir, (host_gitdir / "commondir").read_text(encoding="utf-8").strip())
    )
    for name in _SHARED_ENTRIES:
        entry = view.resolve(f"{common}/{name}")
        if entry is None or not entry[0].exists():
            problems.append(f"shared entry {name} is not visible")
        elif not entry[1]:
            problems.append(f"shared entry {name} is writable")
    return problems


def _problems_of_every_candidate(
    tmp_path: Path, make_environment: object, candidates: int
) -> list[str]:
    engine = daemon_engine(tmp_path, launcher=FakePipedProcessLauncher())
    backend = DaemonBackend(engine, threads=SimThreads())
    connector = FakeConnector(tmp_path / "cluster")
    environment = make_environment(tmp_path, engine)  # ty: ignore[call-non-callable]
    found: list[str] = []

    def open_session(request: RunEnvironmentRequest) -> RunEnvironmentSession:
        return open_run_environment(environment, request)

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        opened = [await workspaces.create_candidate() for _ in range(candidates)]
        containers = engine.containers()
        for candidate in opened:
            view = _owner_of(containers, candidate.path)
            found.extend(_git_lookup_problems(view, candidate.path))
            for other in opened:
                if other is candidate:
                    continue
                host_gitdir = _gitdir_of(other.path)
                if view.exposes_at_its_host_path(other.path) or view.exposes_at_its_host_path(
                    host_gitdir
                ):
                    found.append("a candidate's container reaches another candidate")
            common = _gitdir_of(candidate.path).parent.parent
            if any(w == common or w.is_relative_to(common) for w in view.writable_hosts()):
                found.append("a candidate's container can write the shared repository")

    world = tmp_path / "world"
    world.mkdir()
    _on_the_hosts_threads(world, backend, open_session, connector, exercise)
    return found


def _on_the_hosts_threads(
    world: Path,
    backend: DaemonBackend,
    open_session: Callable[[RunEnvironmentRequest], RunEnvironmentSession],
    connector: FakeConnector,
    exercise: Callable[[RuntimeWorkspaces], Coroutine[object, object, None]],
) -> None:
    """Run ``exercise`` on the first host, as a simulated thread of the hosts' own threads.

    A local Slurm root starts its workspace-root probe on a thread of the host, so the
    hosts' threads must run: the probe proves the Fake cluster's root and ends.
    """
    threads = SimThreads()

    def run() -> None:
        with open_workspace_env(
            world,
            git=GitKind.REAL,
            backend=backend,
            open_session=open_session,
            owns_candidate_worktrees=True,
            system=replace(idle_environment_system(slurm_processes=connector), threads=threads),
        ) as env:
            try:
                asyncio.run(exercise(env.hosts[0]))
            finally:
                for host in reversed(env.hosts):
                    with suppress(Exception):
                        asyncio.run(host.close())

    try:
        threads.run(run)
    finally:
        threads.close()


def _owner_of(containers: tuple[FakeContainer, ...], workspace: Path) -> _ContainerView:
    owners = [c for c in containers if workspace in c.mounts.values()]
    assert len(owners) == 1
    return _ContainerView(owners[0])


def _gitdir_of(workspace: Path) -> Path:
    pointer = (workspace / ".git").read_text(encoding="utf-8").strip()
    return Path(pointer.removeprefix("gitdir:").strip())


@pytest.mark.parametrize("make_environment", [_docker, _slurm], ids=["docker", "slurm"])
def test_a_candidate_container_can_use_git_in_its_worktree_and_reaches_no_other_candidate(
    tmp_path: Path, make_environment: object
) -> None:
    assert _problems_of_every_candidate(tmp_path, make_environment, candidates=3) == []


# A fake token, not a real credential: proves no mounted file carries it, not that this
# exact string is unsafe.
_EMBEDDED_MARKER = "sshh-do-not-leak-fdbb21"


@pytest.mark.parametrize("make_environment", [_docker, _slurm], ids=["docker", "slurm"])
def test_a_candidate_container_cannot_read_the_shared_repository_s_credential(
    tmp_path: Path, make_environment: object
) -> None:
    """Regression for #2101.

    The shared repository's Git ``config`` can hold a remote URL of the form
    ``https://user:token@host/...``, embedding a credential. A candidate's
    container must never see it, in a file mounted at any path.
    """
    engine = daemon_engine(tmp_path, launcher=FakePipedProcessLauncher())
    backend = DaemonBackend(engine, threads=SimThreads())
    connector = FakeConnector(tmp_path / "cluster")
    environment = make_environment(tmp_path, engine)  # ty: ignore[call-non-callable]
    views: list[_ContainerView] = []

    def open_session(request: RunEnvironmentRequest) -> RunEnvironmentSession:
        return open_run_environment(environment, request)

    async def exercise(workspaces: RuntimeWorkspaces) -> None:
        run_test_command(
            [
                "git",
                "remote",
                "add",
                "origin",
                f"https://user:{_EMBEDDED_MARKER}@example.invalid/repo.git",
            ],
            cwd=workspaces.root.path,
            check=True,
            capture_output=True,
        )
        candidate = await workspaces.create_candidate()
        views.append(_owner_of(engine.containers(), candidate.path))

    world = tmp_path / "world"
    world.mkdir()
    _on_the_hosts_threads(world, backend, open_session, connector, exercise)

    (view,) = views
    leaked = [
        path
        for path in view.mounted_host_files()
        if _EMBEDDED_MARKER in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert leaked == []
