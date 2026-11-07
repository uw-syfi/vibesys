"""Tracked repository files for check scanners.

``git ls-files`` lists this repository's index only. Checked-out submodule
trees are therefore invisible to scanners that use this helper: a submodule
appears as a single gitlink path, never as the files inside it.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

__all__ = ["RepoFilesError", "tracked_files", "tracked_paths"]


class RepoFilesError(RuntimeError):
    """Raised when the repository file listing cannot be produced."""

    def __init__(self, repo_root: Path, detail: str) -> None:
        """Name the repository root and the underlying listing failure."""
        super().__init__(f"git ls-files failed under {repo_root}: {detail}")
        self.repo_root = repo_root
        self.detail = detail


def tracked_paths(
    repo_root: Path, *, names: Sequence[str] | None = None
) -> tuple[str, ...]:
    """Return every repo-relative path ``git ls-files`` reports for ``repo_root``.

    ``names`` injects a listing for tests. When ``repo_root`` is not a git work
    tree (throwaway fixture directories), the working tree is listed instead so
    scanner unit tests need neither git nor an injected listing.
    """
    if names is not None:
        return tuple(names)
    git = shutil.which("git")
    if git is None:
        raise RepoFilesError(repo_root, "git is not on PATH")
    # lint-waiver: LW-389001 [S603]; Fixed git argv, repo root path only, no shell.
    result = subprocess.run(  # noqa: S603
        [git, "-C", str(repo_root), "ls-files", "-z"],
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return tuple(path for path in result.stdout.decode("utf-8").split("\0") if path)
    detail = result.stderr.decode("utf-8", errors="replace").strip() or (
        f"exit {result.returncode}"
    )
    if "not a git repository" not in detail:
        raise RepoFilesError(repo_root, detail)
    return tuple(
        sorted(
            path.relative_to(repo_root).as_posix()
            for path in repo_root.rglob("*")
            if path.is_file()
        )
    )


def tracked_files(
    repo_root: Path,
    *,
    suffixes: Sequence[str] | None = None,
    under: Sequence[str] | None = None,
    names: Sequence[str] | None = None,
) -> tuple[Path, ...]:
    """Absolute paths of tracked files, optionally filtered by suffix and prefix.

    Submodule gitlinks appear in ``git ls-files`` as one path. When that path is
    a directory on disk it fails ``is_file``, so submodule contents are never
    returned. ``names`` injects a listing for tests (see ``tracked_paths``).
    """
    suffix_filter = tuple(suffixes) if suffixes is not None else None
    selected: list[Path] = []
    for relative in tracked_paths(repo_root, names=names):
        if under is not None and not _under_any(relative, under):
            continue
        if suffix_filter is not None and not relative.endswith(suffix_filter):
            continue
        path = repo_root / relative
        if path.is_file():
            selected.append(path)
    return tuple(sorted(selected))


def _under_any(relative: str, roots: Sequence[str]) -> bool:
    """Whether ``relative`` is exactly one of ``roots`` or lives under one."""
    for root in roots:
        normalized = root.rstrip("/")
        if relative == normalized or relative.startswith(f"{normalized}/"):
            return True
    return False
