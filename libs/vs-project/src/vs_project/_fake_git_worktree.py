"""The working-tree side of the in-memory Git: reading, writing, and walking real files.

The index and history of ``FakeGitRepository`` live in memory, but the working tree is
the project's real directory, because tests and agents write files there with ordinary
file APIs. This module is the only place the Fake touches those files.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from vs_project._git_ignore import IgnoreLayer, IgnoreLayers, parse_ignore_lines
from vs_project._git_objects import EXECUTABLE, REGULAR, SYMLINK, FileEntry, blob_id

if TYPE_CHECKING:
    from collections.abc import Iterator, Set

    from vs_project._git_pathspec import Pathspecs

GITIGNORE = ".gitignore"


class UnreadablePathError(OSError):
    """A working-tree file exists but cannot be read."""

    def __init__(self, path: str) -> None:
        """Name the unreadable ``path`` (relative to the working-tree root)."""
        super().__init__(f"cannot read {path}")
        self.path = path


def read_file(root: Path, relative: str) -> tuple[FileEntry, bytes] | None:
    """The entry and bytes of the file or symlink at ``relative``; ``None`` if there is none.

    Raises ``UnreadablePathError`` for a file the process may not read.
    """
    path = root / relative
    try:
        info = path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    if stat.S_ISLNK(info.st_mode):
        data = os.fsencode(path.readlink())
        return FileEntry(SYMLINK, blob_id(data)), data
    if not stat.S_ISREG(info.st_mode):
        return None
    try:
        data = path.read_bytes()
    except PermissionError as error:
        raise UnreadablePathError(relative) from error
    except (FileNotFoundError, NotADirectoryError):
        return None
    mode = EXECUTABLE if info.st_mode & stat.S_IXUSR else REGULAR
    return FileEntry(mode, blob_id(data)), data


def read_entry(root: Path, relative: str) -> FileEntry | None:
    """Like ``read_file`` without the bytes."""
    found = read_file(root, relative)
    return None if found is None else found[0]


def write_file(root: Path, relative: str, entry: FileEntry, data: bytes) -> None:
    """Make ``relative`` hold ``entry``/``data``, replacing whatever stands in its way."""
    path = root / relative
    _clear_obstructions(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.is_file():
        path.unlink()
    if entry.is_symlink():
        path.symlink_to(os.fsdecode(data))
        return
    path.write_bytes(data)
    path.chmod(0o755 if entry.mode == EXECUTABLE else 0o644)


def _clear_obstructions(root: Path, path: Path) -> None:
    """Remove a file where a parent directory must be, and a directory where a file must be."""
    for parent in reversed(path.relative_to(root).parents):
        candidate = root / parent
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink()
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)


def remove_file(root: Path, relative: str) -> None:
    """Delete ``relative`` and any directories it leaves empty (never the root itself)."""
    path = root / relative
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    for parent in Path(relative).parents:
        if str(parent) == ".":
            return
        try:
            (root / parent).rmdir()
        except OSError:
            return


@dataclass(frozen=True)
class Found:
    """A working-tree path that no index entry covers."""

    path: str
    """Relative POSIX path; a directory that is ignored as a whole ends with ``/``."""
    ignored: bool


def baseline_layers(exclude_lines: list[str], extra: list[str] | None = None) -> IgnoreLayers:
    """The ignore layers outside any directory: command-line ``extra`` patterns, then excludes."""
    layers = IgnoreLayers()
    layers = layers.below(IgnoreLayer("", parse_ignore_lines(exclude_lines)))
    if extra:
        layers = layers.below(IgnoreLayer("", parse_ignore_lines(extra)))
    return layers


@dataclass(frozen=True)
class WalkRequest:
    """What ``walk_untracked`` should look for."""

    root: Path
    tracked: Set[str]
    layers: IgnoreLayers
    specs: Pathspecs | None = None
    use_ignore_files: bool = True
    descend_ignored: bool = False
    """Report the files inside ignored directories (as ignored) instead of the directory."""
    skip: Set[str] = frozenset()
    """Directories (relative) that belong to another worktree and are left alone."""


def walk_untracked(request: WalkRequest) -> Iterator[Found]:
    """Every working-tree file the index does not track, with whether the rules ignore it."""
    yield from _walk(request, "", request.layers, inherited_ignore=False)


def _children(directory: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as scan:
            return sorted(scan, key=lambda entry: entry.name)
    except OSError:
        return []


def _layers_for(request: WalkRequest, relative: str, layers: IgnoreLayers) -> IgnoreLayers:
    if not request.use_ignore_files:
        return layers
    return _layers_for_dir(request.root, relative, layers)


def _layers_for_dir(root: Path, relative: str, layers: IgnoreLayers) -> IgnoreLayers:
    """``layers`` plus the rules of the ``.gitignore`` in ``relative``, if it has one."""
    try:
        text = (root / relative / GITIGNORE).read_text(encoding="utf-8", errors="surrogateescape")
    except OSError:
        return layers
    return layers.below(IgnoreLayer(relative, parse_ignore_lines(text.splitlines())))


def is_ignored(root: Path, path: str, baseline: IgnoreLayers, *, is_dir: bool = False) -> bool:
    """Whether the rules ignore ``path``, judged by the ignore files along its directories.

    A path below an ignored directory is ignored too.
    """
    parts = path.split("/")
    layers = _layers_for_dir(root, "", baseline)
    for depth in range(1, len(parts) + 1):
        current = "/".join(parts[:depth])
        last = depth == len(parts)
        if layers.ignores(current, is_dir=is_dir if last else True):
            return True
        if not last:
            layers = _layers_for_dir(root, current, layers)
    return False


def _walk(
    request: WalkRequest, relative: str, layers: IgnoreLayers, *, inherited_ignore: bool
) -> Iterator[Found]:
    directory = request.root / relative if relative else request.root
    layers = _layers_for(request, relative, layers)
    for entry in _children(directory):
        if entry.name == ".git":
            continue
        path = f"{relative}/{entry.name}" if relative else entry.name
        if path in request.skip:
            continue
        is_dir = entry.is_dir(follow_symlinks=False)
        ignored = inherited_ignore or layers.ignores(path, is_dir=is_dir)
        if not is_dir:
            if path not in request.tracked and (
                request.specs is None or request.specs.matches(path)
            ):
                yield Found(path, ignored)
            continue
        if Path(entry.path, ".git").exists():
            continue  # a nested repository is opaque
        if request.specs is not None and not request.specs.may_match_below(path):
            continue
        if ignored and not request.descend_ignored:
            yield Found(f"{path}/", ignored=True)
            continue
        yield from _walk(request, path, layers, inherited_ignore=ignored)


def clean_directory(request: WalkRequest, tracked_dirs: Set[str]) -> bool:
    """Delete untracked, non-ignored files and the directories they leave empty.

    Returns whether everything selected could be removed.
    """
    return _clean(request, "", request.layers, tracked_dirs)


def _clean(
    request: WalkRequest, relative: str, layers: IgnoreLayers, tracked_dirs: Set[str]
) -> bool:
    ok = True
    directory = request.root / relative if relative else request.root
    layers = _layers_for(request, relative, layers)
    for entry in _children(directory):
        path = f"{relative}/{entry.name}" if relative else entry.name
        if entry.name == ".git" or path in request.skip:
            continue
        is_dir = entry.is_dir(follow_symlinks=False)
        if layers.ignores(path, is_dir=is_dir):
            continue
        if not is_dir:
            if path not in request.tracked:
                ok &= _unlink(Path(entry.path))
            continue
        if path in request.tracked or Path(entry.path, ".git").exists():
            continue
        ok &= _clean(request, path, layers, tracked_dirs)
        if path not in tracked_dirs:
            with contextlib.suppress(OSError):
                Path(entry.path).rmdir()
    return ok


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except OSError:
        return False
    return True
