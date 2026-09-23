"""Remote-project resolution: cloning a remote project and selecting its run branch."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING

from entrypoints.cli.errors import _configuration_error
from vibesys.api.request import REPOSITORY_SLUG, experiment_origin_matches
from vs_github.api import GitHubCLI, GitHubCLIError
from vs_project.api import Project, ProjectStateError

if TYPE_CHECKING:
    from pathlib import Path


def _is_remote_project(value: str) -> bool:
    return bool(
        REPOSITORY_SLUG.fullmatch(value)
        or value.startswith(("file://", "https://", "ssh://", "git@"))
    )


@dataclass(frozen=True)
class _RemoteRunBranch:
    created_at: float
    run_id: str
    remote_branch: str
    branch: str


def _clone_project(remote: str, runs_dir: Path) -> Path:
    """Clone a remote project into *runs_dir* and return its root."""
    destination = runs_dir / _remote_repository_name(remote)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return _reuse_cloned_project(remote, destination)
    if REPOSITORY_SLUG.fullmatch(remote):
        try:
            GitHubCLI().clone_repository(remote, destination)
        except GitHubCLIError as exc:
            _configuration_error(
                f"Cannot clone project repository {remote!r}: {exc}",
                code="resume_clone_failed",
                stage="resume_resolution",
            )
        return _select_cloned_run_branch(destination.resolve())

    try:
        result = _resume_git(runs_dir, "clone", remote, str(destination))
    except FileNotFoundError as exc:
        _configuration_error(
            f"Cannot clone {remote!r}: required command 'git' is not installed ({exc})",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    _require_resume_git(result, f"Cannot clone project repository {remote!r}")
    return _select_cloned_run_branch(destination.resolve())


def _remote_repository_name(remote: str) -> str:
    repository_name = remote.rstrip("/").rsplit("/", 1)[-1]
    if ":" in repository_name:
        repository_name = repository_name.rsplit(":", 1)[-1]
    repository_name = repository_name.removesuffix(".git")
    if repository_name in {"", ".", ".."}:
        _configuration_error(
            f"Cannot determine a safe local directory name from --resume {remote!r}",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    return repository_name


def _reuse_cloned_project(remote: str, destination: Path) -> Path:
    if not destination.is_dir():
        _configuration_error(
            f"Cannot clone project: destination is not a directory: {destination}",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    existing_origin = _resume_git(destination, "remote", "get-url", "origin")
    expected_origin = remote.removesuffix(".git").rstrip("/")
    actual_origin = existing_origin.stdout.strip().removesuffix(".git").rstrip("/")
    origin_matches = existing_origin.returncode == 0 and (
        experiment_origin_matches(destination, remote)
        if REPOSITORY_SLUG.fullmatch(remote)
        else actual_origin == expected_origin
    )
    if not origin_matches:
        _configuration_error(
            f"Cannot clone {remote!r}: destination already exists with a different origin: "
            f"{destination}",
            code="resume_clone_failed",
            stage="resume_resolution",
        )
    fetched = _resume_git(destination, "fetch", "--prune", "origin")
    _require_resume_git(fetched, f"Cannot update cloned project {destination}")
    return _select_cloned_run_branch(destination.resolve())


def _select_cloned_run_branch(project_root: Path) -> Path:
    """Select the newest valid portable run after cloning its repository."""
    listed = _resume_git(
        project_root,
        "for-each-ref",
        "--format=%(refname:short)",
        "refs/remotes/origin/vibesys-runs/",
        "refs/remotes/origin/vibesys/",
    )
    _require_resume_git(listed, f"Cannot inspect VibeSys run branches in {project_root}")
    candidates = _remote_run_candidates(project_root, listed.stdout.splitlines())
    if not candidates:
        _configuration_error(
            f"No valid VibeSys run branches were found in cloned project {project_root}.",
            code="resume_not_found",
            stage="resume_resolution",
        )

    selected = max(candidates, key=lambda candidate: (candidate.created_at, candidate.run_id))
    _checkout_remote_run_branch(project_root, selected)
    Project.open(project_root).state.set_current_run(selected.run_id)
    return project_root


def _remote_run_candidates(
    project_root: Path,
    remote_branches: list[str],
) -> list[_RemoteRunBranch]:
    candidates: list[_RemoteRunBranch] = []
    for remote_branch in remote_branches:
        identity = _remote_run_identity(remote_branch)
        if identity is None:
            continue
        branch, run_id = identity
        switched = _resume_git(project_root, "switch", "--detach", "--quiet", remote_branch)
        if switched.returncode != 0 or not Project.is_state_initialized(project_root):
            continue
        try:
            manifest = Project.open(project_root).state.load_run(run_id)
        except ProjectStateError:
            continue
        if manifest.branch == branch:
            candidates.append(
                _RemoteRunBranch(
                    created_at=manifest.created_at.timestamp(),
                    run_id=manifest.run_id,
                    remote_branch=remote_branch,
                    branch=branch,
                )
            )
    return candidates


def _remote_run_identity(remote_branch: str) -> tuple[str, str] | None:
    branch = remote_branch.removeprefix("origin/")
    for prefix in ("vibesys-runs/", "vibesys/"):
        if branch.startswith(prefix) and (run_id := branch.removeprefix(prefix)):
            return branch, run_id
    return None


def _checkout_remote_run_branch(project_root: Path, selected: _RemoteRunBranch) -> None:
    local_exists = _resume_git(
        project_root,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{selected.branch}",
    )
    switch_arguments = (
        ("switch", "--quiet", selected.branch)
        if local_exists.returncode == 0
        else (
            "switch",
            "--quiet",
            "--track",
            "-c",
            selected.branch,
            selected.remote_branch,
        )
    )
    switched = _resume_git(project_root, *switch_arguments)
    _require_resume_git(switched, f"Cannot select VibeSys run branch {selected.branch!r}")
    if local_exists.returncode == 0:
        advanced = _resume_git(
            project_root,
            "merge",
            "--quiet",
            "--ff-only",
            selected.remote_branch,
        )
        _require_resume_git(
            advanced,
            f"Cannot fast-forward VibeSys run branch {selected.branch!r}",
        )
    upstream = _resume_git(project_root, "rev-parse", "--abbrev-ref", "@{upstream}")
    if upstream.returncode == 0 and upstream.stdout.strip() == selected.remote_branch:
        return
    tracked = _resume_git(
        project_root,
        "branch",
        "--set-upstream-to",
        selected.remote_branch,
        selected.branch,
    )
    _require_resume_git(tracked, f"Cannot track VibeSys run branch {selected.remote_branch!r}")


def _resume_git(project_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    """Run a non-shell Git command during remote resume resolution."""
    return subprocess.run(  # noqa: PLW1510, S603  # tracked: #288
        ["git", *arguments],  # noqa: S607  # tracked: #288
        cwd=project_root,
        capture_output=True,
        text=True,
    )


def _require_resume_git(
    result: subprocess.CompletedProcess[str],
    message: str,
) -> None:
    if result.returncode == 0:
        return
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    _configuration_error(
        f"{message}: {detail}",
        code="resume_clone_failed",
        stage="resume_resolution",
    )
