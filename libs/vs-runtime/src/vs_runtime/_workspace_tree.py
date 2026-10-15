"""Host reads and writes of an agent-controlled tree, by descriptor from its root.

The agent can replace any directory in its workspace with a symlink at any moment, so a
path the host resolved and checked may lead elsewhere by the time the host opens it.
Every operation here instead walks the relative path one component at a time from a
descriptor of the tree's root: each component is opened with ``openat`` and
``O_NOFOLLOW`` (and ``O_DIRECTORY`` for a directory), and the file is read or written
only through the last descriptor. A symlink anywhere on the path, ``..``, or an absolute
path is refused, so no swap made before or during the walk can lead the host out of the
tree. Linux and macOS give ``openat``, ``mkdirat``, ``renameat`` and ``O_NOFOLLOW`` the
same semantics; Linux's ``openat2(RESOLVE_BENEATH)`` would do the walk in one call, but
Python's ``os`` module does not expose it.
"""

from __future__ import annotations

import errno
import itertools
import os
import stat
from contextlib import contextmanager, suppress
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from vs_project.api import LocalAtomicWriteEffects, atomic_write_bytes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from io import FileIO

    from vs_project.api import AtomicWriteStream

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_TEMPORARY_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_TEMPORARY_ATTEMPTS = 64
_READ_CHUNK_BYTES = 1024 * 1024
_temporary_names = itertools.count()


class WorkspacePathError(ValueError):
    """A relative path that is absolute, holds ``..``, or passes through a symlink."""


class NotRegularFileError(OSError):
    """The path names something other than a regular file (a directory, a FIFO)."""


class EntryKind(StrEnum):
    """What a tree path names, seen without following a symlink."""

    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


def relative_parts(relative: str) -> tuple[str, ...]:
    """The components of ``relative``, the tree root for ``""`` or ``"."``.

    Raises ``WorkspacePathError`` for an absolute path, a ``..`` component or a NUL.
    """
    path = PurePosixPath(relative)
    if path.is_absolute() or "\0" in relative:
        message = f"{relative} escapes the workspace"
        raise WorkspacePathError(message)
    if ".." in path.parts:
        message = f"{relative} escapes the workspace through '..'"
        raise WorkspacePathError(message)
    return path.parts


class WorkspaceTree:
    """An open agent-controlled tree; every path is relative to its root."""

    def __init__(self, root: Path, root_descriptor: int) -> None:
        """Wrap ``root_descriptor``, the descriptor :func:`open_workspace_tree` owns for ``root``.

        ``root`` only names written files in the framework's record of its own writes.
        """
        self._path = root
        self._root = root_descriptor

    def kind(self, relative: str) -> EntryKind | None:
        """What ``relative`` names, or ``None`` when nothing is there."""
        parts = relative_parts(relative)
        if not parts:
            return EntryKind.DIRECTORY
        try:
            with self._parent(parts, create=False) as parent:
                mode = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode
        except (FileNotFoundError, NotADirectoryError):
            return None
        if stat.S_ISLNK(mode):
            return EntryKind.SYMLINK
        if stat.S_ISDIR(mode):
            return EntryKind.DIRECTORY
        return EntryKind.FILE if stat.S_ISREG(mode) else EntryKind.OTHER

    @contextmanager
    def open_file(self, relative: str) -> Iterator[FileIO]:
        """An unbuffered binary stream over the regular file at ``relative``.

        Opened without blocking and checked on the descriptor, so a FIFO is refused
        rather than waited on. Raises ``NotRegularFileError`` for anything but a regular
        file and ``WorkspacePathError`` for a symlink, an ``OSError`` otherwise.
        """
        parts = _file_parts(relative)
        with self._parent(parts, create=False) as parent:
            descriptor = _open_component(parts[-1], _READ_FLAGS, parent, relative)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            os.close(descriptor)
            message = f"{relative} is not a regular file"
            raise NotRegularFileError(errno.EINVAL, message)
        with os.fdopen(descriptor, "rb", buffering=0) as stream:
            yield stream

    def read_bytes(self, relative: str, *, max_bytes: int) -> bytes:
        """At most ``max_bytes + 1`` bytes of the regular file at ``relative``."""
        with self.open_file(relative) as stream:
            chunks: list[bytes] = []
            remaining = max_bytes + 1
            while remaining and (chunk := stream.read(min(remaining, _READ_CHUNK_BYTES))):
                chunks.append(chunk)
                remaining -= len(chunk)
        return b"".join(chunks)

    def write_bytes(self, relative: str, contents: bytes) -> None:
        """Atomically replace the file at ``relative``, creating missing directories.

        The temporary file is created and renamed inside the descriptor of the
        destination's directory, so the bytes land in the tree or nowhere.
        """
        parts = _file_parts(relative)
        with self._parent(parts, create=True) as parent:
            atomic_write_bytes(
                self._path.joinpath(*parts),
                contents,
                effects=_DirectoryAtomicWrite(parent, parts[-1]),
            )

    def files_below(self, relative: str) -> tuple[str, ...]:
        """The regular files below the directory ``relative``, sorted by components.

        Each is named relative to the tree root. Raises ``WorkspacePathError`` for a
        symlink found below it; other entries (FIFOs, sockets) are skipped.
        """
        pending = [relative_parts(relative)]
        files: list[tuple[str, ...]] = []
        while pending:
            parts = pending.pop()
            with self._directory(parts, create=False) as directory, os.scandir(directory) as it:
                for entry in it:
                    child = (*parts, entry.name)
                    if entry.is_symlink():
                        message = f"{PurePosixPath(*child)} is a symlink"
                        raise WorkspacePathError(message)
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(child)
                    elif entry.is_file(follow_symlinks=False):
                        files.append(child)
        return tuple(PurePosixPath(*parts).as_posix() for parts in sorted(files))

    @contextmanager
    def _parent(self, parts: tuple[str, ...], *, create: bool) -> Iterator[int]:
        with self._directory(parts[:-1], create=create) as parent:
            yield parent

    @contextmanager
    def _directory(self, parts: tuple[str, ...], *, create: bool) -> Iterator[int]:
        """A descriptor for the directory ``parts``, walked from the root."""
        current = os.dup(self._root)
        try:
            for depth, name in enumerate(parts):
                shown = PurePosixPath(*parts[: depth + 1]).as_posix()
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(name, dir_fd=current)
                child = _open_component(name, _DIRECTORY_FLAGS, current, shown)
                os.close(current)
                current = child
            yield current
        finally:
            os.close(current)


@contextmanager
def open_workspace_tree(root: Path) -> Iterator[WorkspaceTree]:
    """Open the tree at ``root``, a directory the host owns the path to."""
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        yield WorkspaceTree(root, descriptor)
    finally:
        os.close(descriptor)


def _file_parts(relative: str) -> tuple[str, ...]:
    parts = relative_parts(relative)
    if not parts:
        message = f"{relative!r} names the workspace itself, not a file"
        raise WorkspacePathError(message)
    return parts


def _open_component(name: str, flags: int, directory: int, shown: str) -> int:
    """Open one path component in ``directory``; a symlink there is refused."""
    try:
        return os.open(name, flags, dir_fd=directory)
    except OSError as error:
        # ``O_NOFOLLOW`` refuses a symlink with ELOOP, or with ENOTDIR on Linux when
        # ``O_DIRECTORY`` is set too; either may also mean something else.
        if error.errno not in {errno.ELOOP, errno.ENOTDIR}:
            raise
        if not stat.S_ISLNK(os.stat(name, dir_fd=directory, follow_symlinks=False).st_mode):
            raise
        message = f"{shown} is a symlink"
        raise WorkspacePathError(message) from error


class _DirectoryAtomicWrite:
    """``AtomicWriteEffects`` that stage and publish ``name`` inside one directory descriptor.

    The paths ``atomic_write_bytes`` passes are bookkeeping only; every operation uses
    ``directory`` and a bare name, so none of them resolves a path again.
    """

    def __init__(self, directory: int, name: str) -> None:
        self._directory = directory
        self._name = name

    @contextmanager
    def temporary(self, destination: Path) -> Iterator[tuple[Path, AtomicWriteStream]]:
        del destination
        for _ in range(_TEMPORARY_ATTEMPTS):
            temporary = f".{self._name}.{next(_temporary_names)}.tmp"
            try:
                descriptor = os.open(temporary, _TEMPORARY_FLAGS, 0o600, dir_fd=self._directory)
            except FileExistsError:
                continue
            with os.fdopen(descriptor, "wb") as stream:
                yield Path(temporary), stream
            return
        raise FileExistsError(errno.EEXIST, "no free temporary name", self._name)

    def sync_file(self, stream: AtomicWriteStream) -> None:
        LocalAtomicWriteEffects().sync_file(stream)

    def replace(self, temporary: Path, destination: Path) -> None:
        del destination
        os.replace(
            temporary.name, self._name, src_dir_fd=self._directory, dst_dir_fd=self._directory
        )

    def sync_directory(self, directory: Path) -> None:
        del directory
        os.fsync(self._directory)

    def remove_temporary(self, temporary: Path) -> None:
        with suppress(FileNotFoundError):
            os.unlink(temporary.name, dir_fd=self._directory)


__all__ = [
    "EntryKind",
    "NotRegularFileError",
    "WorkspacePathError",
    "WorkspaceTree",
    "open_workspace_tree",
    "relative_parts",
]
