"""Folder browsing, project validation, and the recent-projects list."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.web_home.contract import ApiError, ErrorCode, FsEntry, FsListing

if TYPE_CHECKING:
    from entrypoints.web_home.context import HomeConfig, Request


def confine(config: HomeConfig, raw: str) -> Path:
    """Return the canonical form of absolute *raw*, or reject it outside the granted roots.

    Symlinks are resolved before the containment check, so a link inside a
    root that points outside it is rejected like the target itself.
    """
    lexical = Path(raw).expanduser()
    if not raw or "\0" in raw or not lexical.is_absolute():
        message = "path must be an absolute path"
        raise ApiError(ErrorCode.INVALID_PATH, message)
    try:
        path = lexical.resolve()
    except OSError as error:
        message = f"cannot resolve {lexical}: {error.strerror}"
        raise ApiError(ErrorCode.INVALID_PATH, message) from None
    if not _within_roots(config, path):
        message = f"{path} is outside the folders this app may open"
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
    except OSError:
        return None


def _is_git(path: Path) -> bool:
    try:
        return (path / ".git").exists()
    except OSError:
        return False
