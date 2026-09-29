"""Folder browsing, project validation, and the recent-projects list."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from entrypoints.web_home.context import atomic_write, git, parse_body, pending_changes
from entrypoints.web_home.contract import (
    ApiError,
    ErrorCode,
    FsEntry,
    FsListing,
    ProjectList,
    ProjectRef,
    ProjectState,
    ProjectValidation,
    RecentProject,
    ValidateRequest,
)
from vs_project.api import Project, ProjectError

if TYPE_CHECKING:
    from entrypoints.web_home.context import HomeConfig, Request

_MAX_RECENTS = 20


def confine(config: HomeConfig, raw: str) -> Path:
    """Return the canonical form of absolute *raw*, or reject it outside the granted roots.

    Symlinks are resolved before the containment check, so a link inside a
    root that points outside it is rejected like the target itself. The
    empty/NUL/absolute checks run before any expansion, so a malformed
    home-relative input (``~unknownuser``, a ``~`` followed by a NUL byte)
    is rejected as ``invalid_path`` instead of raising out of ``expanduser()``.
    """
    if not raw or "\0" in raw or not Path(raw).is_absolute():
        message = "path must be an absolute path"
        raise ApiError(ErrorCode.INVALID_PATH, message)
    try:
        path = Path(raw).resolve()
    except (OSError, RuntimeError):
        # RuntimeError: a symlink loop. Path.resolve() raises RuntimeError for
        # this on Python 3.12 (our floor); only 3.13+ raises OSError.
        message = "path cannot be resolved"
        raise ApiError(ErrorCode.INVALID_PATH, message) from None
    if not _within_roots(config, path):
        message = "path is outside the folders this app may open"
        raise ApiError(ErrorCode.OUTSIDE_ROOTS, message)
    return path


def _within_roots(config: HomeConfig, path: Path) -> bool:
    return any(path.is_relative_to(root) for root in config.roots)


def list_directory(request: Request) -> FsListing:
    """``GET /api/fs?path=``: subfolders of *path*, or the granted roots without one."""
    config = request.config
    raw = request.arg("path")
    if raw is None:
        return FsListing(
            path=None,
            parent=None,
            entries=[
                FsEntry(name=str(root), path=str(root), git=_is_git(root)) for root in config.roots
            ],
        )
    directory = confine(config, raw)
    if not directory.is_dir():
        message = f"not a directory: {directory}"
        raise ApiError(ErrorCode.INVALID_PATH, message)
    try:
        children = sorted(directory.iterdir(), key=lambda child: child.name.lower())
    except PermissionError:
        message = f"permission denied: {directory}"
        raise ApiError(ErrorCode.PERMISSION_DENIED, message) from None
    parent = directory.parent
    return FsListing(
        path=str(directory),
        parent=str(parent) if parent != directory and _within_roots(config, parent) else None,
        entries=[entry for child in children if (entry := _entry(config, child)) is not None],
    )


def _entry(config: HomeConfig, child: Path) -> FsEntry | None:
    if child.name.startswith("."):
        return None
    try:
        target = child.resolve(strict=True)
        if not target.is_dir() or not _within_roots(config, target):
            return None
        return FsEntry(name=child.name, path=str(target), git=_is_git(target))
    except (OSError, RuntimeError):
        # RuntimeError: a symlink loop (see confine); drop just this entry.
        return None


def _is_git(path: Path) -> bool:
    try:
        return (path / ".git").exists()
    except OSError:
        return False


def project_id(root: Path) -> str:
    """Return the stable URL id of a canonical project root."""
    return hashlib.sha256(str(root).encode()).hexdigest()[:16]


def inspect_project(path: Path) -> ProjectValidation:
    """Classify *path* by its first launch blocker, in the order the UI resolves them."""
    if not path.is_dir():
        return ProjectValidation(state=ProjectState.MISSING, path=str(path))
    if git(path, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
        return ProjectValidation(state=ProjectState.NOT_GIT, path=str(path))
    ref = ProjectRef(id=project_id(path), root=str(path), name=path.name)
    try:
        project = Project.open(path)
        initialized = project.is_initialized()
        tasks = [task.name.value for task in project.discover_tasks()] if initialized else []
    except ProjectError as error:
        return ProjectValidation(
            state=ProjectState.INVALID, path=str(path), project=ref, message=str(error)
        )
    has_commits = git(path, "rev-parse", "--verify", "--quiet", "HEAD").returncode == 0
    pending = pending_changes(path) if has_commits else []
    if not initialized:
        state = ProjectState.UNINITIALIZED
    elif not tasks:
        state = ProjectState.NO_TASKS
    elif not has_commits:
        state = ProjectState.NO_COMMITS
    elif pending:
        state = ProjectState.DIRTY_TREE
    else:
        state = ProjectState.READY
    return ProjectValidation(state=state, path=str(path), project=ref, tasks=tasks, pending=pending)


def validate(request: Request) -> ProjectValidation:
    """``POST /api/projects/validate``: classify a folder and remember git work trees."""
    body = parse_body(request, ValidateRequest)
    validation = inspect_project(confine(request.config, body.path))
    if validation.project is not None:
        remember(request.config, validation.project)
    return validation


def _recents_path(config: HomeConfig) -> Path:
    return config.state_home / "web" / "recent-projects.json"


def _load_recents(config: HomeConfig) -> list[RecentProject]:
    try:
        return ProjectList.model_validate_json(_recents_path(config).read_bytes()).projects
    except (OSError, ValidationError):
        return []


def remember(config: HomeConfig, project: ProjectRef) -> None:
    """Move *project* to the front of the recent-projects list."""
    entry = RecentProject(**project.model_dump(), last_opened=config.clock().isoformat())
    with config.write_lock:
        kept = [item for item in _load_recents(config) if item.id != project.id]
        document = ProjectList(projects=[entry, *kept][:_MAX_RECENTS])
        exclude = {"home": True, "projects": {"__all__": {"missing"}}}
        encoded = document.model_dump_json(indent=2, exclude=exclude).encode()
        atomic_write(_recents_path(config), encoded, mode=0o600)


def recent(request: Request) -> ProjectList:
    """``GET /api/projects``: recent projects, most recent first, and the user's home."""
    config = request.config
    home = config.environ.get("HOME") or str(Path.home())
    projects = [
        entry.model_copy(update={"missing": not Path(entry.root).is_dir()})
        for entry in _load_recents(config)
    ]
    return ProjectList(projects=projects, home=home)


def resolve_project(config: HomeConfig, key: str) -> Path:
    """Return the canonical root of a remembered project id, still inside the roots."""
    for entry in _load_recents(config):
        if entry.id == key:
            return confine(config, entry.root)
    message = f"unknown project id {key!r}; validate the folder first"
    raise ApiError(ErrorCode.UNKNOWN_PROJECT, message)
