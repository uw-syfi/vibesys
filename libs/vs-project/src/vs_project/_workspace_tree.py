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
import os
import shutil
import stat
from collections.abc import Callable, Collection
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from vs_project._state_io import LocalAtomicWriteEffects, atomic_write_bytes

if TYPE_CHECKING:
    from collections.abc import Iterator
    from io import FileIO

    from vs_project._state_io import AtomicWriteStream

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
# No ``O_TRUNC``: the file is checked to be regular before it is truncated.
_OVERWRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_TEMPORARY_ATTEMPTS = 64
_READ_CHUNK_BYTES = 1024 * 1024


class TreeLimitError(ValueError):
    """A walk found more entries than its caller allowed."""


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


type CopyIgnore = Callable[[str, list[str]], Collection[str]]
"""Like ``shutil.copytree``'s ``ignore``: a source directory and names, to the names to skip."""


@dataclass(frozen=True, slots=True)
class CopiedTree:
    """What :meth:`WorkspaceTree.copy_in` copied, relative to the copied directory."""

    copied: frozenset[PurePosixPath]
    """Every directory and regular file copied."""
    links: tuple[PurePosixPath, ...]
    """The source symlinks not ignored, which were not copied."""


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

    def files_below(self, relative: str, *, limit: int | None = None) -> tuple[str, ...]:
        """The regular files below the directory ``relative``, sorted by components.

        Each is named relative to the tree root. Raises ``WorkspacePathError`` for a
        symlink found below it; other entries (FIFOs, sockets) are skipped. With a
        ``limit``, raises ``TreeLimitError`` as soon as the walk finds more than
        ``limit`` files or more than ``limit`` directories, without listing the rest.
        """
        pending = [relative_parts(relative)]
        files: list[tuple[str, ...]] = []
        directories = 0
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
                        directories += 1
                    elif entry.is_file(follow_symlinks=False):
                        files.append(child)
                    if limit is not None and max(len(files), directories) > limit:
                        message = f"{relative} holds more than {limit} files or directories"
                        raise TreeLimitError(message)
        return tuple(PurePosixPath(*parts).as_posix() for parts in sorted(files))

    def make_directories(self, relative: str) -> None:
        """Create the directory ``relative`` and any missing directories above it."""
        with self._directory(relative_parts(relative), create=True):
            pass

    def remove(self, relative: str) -> None:
        """Remove whatever ``relative`` names, a directory with everything below it.

        A symlink is removed itself, never followed, at the end of the path and
        anywhere below it; one on the way to it is refused. Missing is not an error.
        """
        parts = _file_parts(relative)
        try:
            with self._parent(parts, create=False) as parent:
                mode = os.stat(parts[-1], dir_fd=parent, follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    # The descriptor-based rmtree opens every directory below with
                    # ``O_NOFOLLOW`` and removes entries by name inside it.
                    shutil.rmtree(parts[-1], dir_fd=parent)
                else:
                    os.unlink(parts[-1], dir_fd=parent)
        except (FileNotFoundError, NotADirectoryError):
            return

    def overwrite(self, relative: str, contents: bytes) -> None:
        """Write ``contents`` into the file at ``relative`` in place, creating it if missing.

        Unlike :meth:`write_bytes` the file keeps its inode, for a reader that holds it
        open. Missing directories are created. Raises ``WorkspacePathError`` for a symlink
        and ``NotRegularFileError`` for anything but a regular file.
        """
        parts = _file_parts(relative)
        with self._parent(parts, create=True) as parent:
            descriptor = _open_component(parts[-1], _OVERWRITE_FLAGS, parent, relative)
        with os.fdopen(descriptor, "wb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                message = f"{relative} is not a regular file"
                raise NotRegularFileError(errno.EINVAL, message)
            stream.truncate(0)
            stream.write(contents)

    def symlink(self, relative: str, target: str) -> None:
        """Create the symlink ``relative`` with the contents ``target``; never followed here."""
        parts = _file_parts(relative)
        with self._parent(parts, create=False) as parent:
            os.symlink(target, parts[-1], dir_fd=parent)

    def names(self, relative: str) -> tuple[str, ...]:
        """The entry names of the directory ``relative``, sorted."""
        parts = relative_parts(relative)
        with self._directory(parts, create=False) as directory, os.scandir(directory) as entries:
            return tuple(sorted(entry.name for entry in entries))

    def symlinks_below(self, relative: str) -> tuple[str, ...]:
        """The symlinks at any depth below the directory ``relative``, sorted.

        Each is named relative to the tree root; no symlink is followed.
        """
        pending = [relative_parts(relative)]
        links: list[tuple[str, ...]] = []
        while pending:
            parts = pending.pop()
            with self._directory(parts, create=False) as directory, os.scandir(directory) as it:
                for entry in it:
                    child = (*parts, entry.name)
                    if entry.is_symlink():
                        links.append(child)
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(child)
        return tuple(PurePosixPath(*parts).as_posix() for parts in sorted(links))

    def entries_named(
        self,
        name: str,
        *,
        limit: int,
        below: str = "",
        skip: Collection[str] = frozenset(),
    ) -> tuple[str, ...]:
        """Every entry of any kind called ``name`` below the directory ``below``, sorted.

        Each is named relative to the tree root. No symlink is followed, and neither an
        entry called ``name`` nor a directory in ``skip`` (root-relative) is entered.
        Nothing is found when ``below`` is not a directory. A directory removed or
        replaced while the walk runs is skipped, as if the walk had passed it a moment
        later, and so is one the host cannot read, which Git cannot read either. Raises
        ``TreeLimitError`` as soon as the walk has seen more than ``limit`` entries,
        without looking at the rest.
        """
        pending: list[tuple[str, ...]] = [relative_parts(below)]
        found: list[tuple[str, ...]] = []
        seen = 0
        while pending:
            parts = pending.pop()
            try:
                with (
                    self._directory(parts, create=False) as directory,
                    os.scandir(directory) as it,
                ):
                    entries = [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in it]
            except (FileNotFoundError, NotADirectoryError, PermissionError, WorkspacePathError):
                continue
            seen += len(entries)
            if seen > limit:
                message = f"the tree holds more than {limit} entries"
                raise TreeLimitError(message)
            for entry_name, is_directory in entries:
                child = (*parts, entry_name)
                if entry_name == name:
                    found.append(child)
                elif is_directory and PurePosixPath(*child).as_posix() not in skip:
                    pending.append(child)
        return tuple(PurePosixPath(*parts).as_posix() for parts in sorted(found))

    def read_link(self, relative: str) -> str:
        """The contents of the symlink ``relative``."""
        parts = _file_parts(relative)
        with self._parent(parts, create=False) as parent:
            return os.readlink(parts[-1], dir_fd=parent)

    def copy_file_in(self, source: Path, relative: str) -> None:
        """Copy the host file ``source`` to the new file ``relative``, keeping its mode."""
        parts = _file_parts(relative)
        with self._parent(parts, create=False) as parent:
            _copy_file(source, parent, parts[-1])

    def copy_in(self, source: Path, relative: str, *, ignore: CopyIgnore) -> CopiedTree:
        """Copy the host directory ``source`` to the new directory ``relative``.

        ``source`` is host-owned and read by path; ``relative`` must not exist and is
        filled only through descriptors opened below it. Regular files keep their mode
        bits and times, directories are created fresh, and symlinks are not copied but
        listed for the caller to recreate with :meth:`symlink` once it knows what was
        copied. ``ignore`` is called like ``shutil.copytree``'s: with a source directory
        and its entry names, returning the names to skip.
        """
        parts = _file_parts(relative)
        copy = _TreeCopy(ignore)
        with self._parent(parts, create=False) as parent:
            descriptor = _make_directory(parent, parts[-1])
            try:
                copy.directory(source, descriptor, PurePosixPath())
            finally:
                os.close(descriptor)
        return CopiedTree(copied=frozenset(copy.copied), links=tuple(sorted(copy.links)))

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


def _make_directory(parent: int, name: str) -> int:
    """Create ``name`` in ``parent`` and open it; an existing entry is refused."""
    os.mkdir(name, dir_fd=parent)
    return _open_component(name, _DIRECTORY_FLAGS, parent, name)


class _TreeCopy:
    """One :meth:`WorkspaceTree.copy_in`: what it copied and the links it left out."""

    def __init__(self, ignore: CopyIgnore) -> None:
        self._ignore = ignore
        self.copied: list[PurePosixPath] = []
        self.links: list[PurePosixPath] = []

    def directory(self, source: Path, descriptor: int, inside: PurePosixPath) -> None:
        """Copy the entries of the host directory ``source`` into ``descriptor``."""
        with os.scandir(source) as it:
            entries = list(it)
        skipped = self._ignore(os.fspath(source), [entry.name for entry in entries])
        for entry in entries:
            if entry.name in skipped:
                continue
            child = inside / entry.name
            if entry.is_symlink():
                self.links.append(child)
            elif entry.is_dir(follow_symlinks=False):
                made = _make_directory(descriptor, entry.name)
                try:
                    self.directory(Path(entry.path), made, child)
                finally:
                    os.close(made)
                self.copied.append(child)
            elif entry.is_file(follow_symlinks=False):
                _copy_file(Path(entry.path), descriptor, entry.name)
                self.copied.append(child)


def _copy_file(source: Path, directory: int, name: str) -> None:
    """Copy the host file ``source`` to the new file ``name`` in ``directory``."""
    with source.open("rb") as reader:
        status = os.fstat(reader.fileno())
        descriptor = os.open(name, _CREATE_FLAGS, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb") as writer:
            shutil.copyfileobj(reader, writer, _READ_CHUNK_BYTES)
            writer.flush()
            os.fchmod(writer.fileno(), stat.S_IMODE(status.st_mode))
            os.utime(writer.fileno(), ns=(status.st_atime_ns, status.st_mtime_ns))


def _open_component(name: str, flags: int, directory: int, shown: str, mode: int = 0o666) -> int:
    """Open one path component in ``directory``; a symlink there is refused.

    ``mode`` applies, under the umask, to a file ``O_CREAT`` creates.
    """
    try:
        return os.open(name, flags, mode, dir_fd=directory)
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
            # Unpredictable, so the agent cannot plant every name the host tries first.
            temporary = f".{self._name}.{os.urandom(8).hex()}.tmp"
            try:
                descriptor = os.open(temporary, _CREATE_FLAGS, 0o600, dir_fd=self._directory)
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
    "CopiedTree",
    "CopyIgnore",
    "EntryKind",
    "NotRegularFileError",
    "WorkspacePathError",
    "WorkspaceTree",
    "open_workspace_tree",
    "relative_parts",
]
