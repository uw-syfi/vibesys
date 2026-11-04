"""Where a store keeps its files: a directory on this host, or memory.

Both writer and reader reach the bytes only through a :class:`Volume`; the reader sees
them as :class:`~vs_sim.api.ByteSource` values, so it never assumes local files. Names are
relative, ``/``-separated, and built only by the store from validated stream ids.
"""

from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING, Protocol

from vs_sim.api import LocalFileSource, SourceWatch, check_read

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import ByteSource


class Volume(Protocol):
    """The files of one store."""

    def append(self, name: str, data: bytes) -> None:
        """Append ``data`` to the file ``name``, creating it and its directory."""
        ...

    def replace(self, name: str, data: bytes) -> None:
        """Atomically make ``data`` the content of ``name``."""
        ...

    def remove(self, name: str) -> None:
        """Delete the file ``name`` if it exists."""
        ...

    def truncate(self, name: str, size: int) -> None:
        """Cut the file ``name`` to its first ``size`` bytes."""
        ...

    def names(self, directory: str) -> list[str]:
        """The file names directly in ``directory``; empty when it does not exist."""
        ...

    def source(self, name: str) -> ByteSource:
        """A fresh source over ``name``; reading a missing one raises ``FileNotFoundError``."""
        ...


class DirectoryVolume:
    """Files under one directory on this host."""

    def __init__(self, root: Path) -> None:
        """Keep files under ``root``."""
        self._root = root

    def append(self, name: str, data: bytes) -> None:
        """Append with one write call, so a reader sees the bytes of a call contiguously."""
        path = self._root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            written = stream.write(data)
        if written != len(data):
            message = f"incomplete log write to {path}: {written} of {len(data)} bytes"
            raise OSError(message)

    def replace(self, name: str, data: bytes) -> None:
        """Write a temporary file, flush it to disk, and rename it over ``name``."""
        path = self._root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)

    def remove(self, name: str) -> None:
        """Unlink ``name``; a missing file is already removed."""
        (self._root / name).unlink(missing_ok=True)

    def truncate(self, name: str, size: int) -> None:
        """Truncate ``name`` in place; it needs no free space."""
        os.truncate(self._root / name, size)

    def names(self, directory: str) -> list[str]:
        """File names in ``directory``."""
        path = self._root / directory
        if not path.is_dir():
            return []
        return sorted(entry.name for entry in path.iterdir() if entry.is_file())

    def source(self, name: str) -> ByteSource:
        """A :class:`~vs_sim.api.LocalFileSource` over ``name``."""
        return LocalFileSource(self._root / name)


class _MemoryFile:
    def __init__(self) -> None:
        self.data = bytearray()
        self.generation = 0


class _MemorySource:
    """Reads one memory file, reporting a replacement or removal as a file source does."""

    def __init__(self, files: dict[str, _MemoryFile], name: str) -> None:
        self._files = files
        self._name = name
        self._watch = SourceWatch(f"memory:{name}")

    def _file(self) -> _MemoryFile:
        found = self._files.get(self._name)
        if found is None:
            self._watch.lost()
            raise FileNotFoundError(self._name)
        self._watch.observe((id(found), found.generation), len(found.data))
        return found

    def size(self) -> int:
        return len(self._file().data)

    def read(self, offset: int, max_bytes: int) -> bytes:
        check_read(offset, max_bytes)
        return bytes(self._file().data[offset : offset + max_bytes])


class MemoryVolume:
    """Files in memory, with the same visibility rules as :class:`DirectoryVolume`.

    With a ``capacity`` it is a disk that fills up: an append that does not fit writes
    the bytes that do and raises ``OSError`` (``ENOSPC``), as a short write to a full
    disk does, and a replacement that does not fit changes nothing and raises. A test
    sets ``capacity`` at any time, to fill the disk while a store writes.
    """

    def __init__(self, *, capacity: int | None = None) -> None:
        """Start empty, holding at most ``capacity`` bytes in all (no limit when ``None``)."""
        self._files: dict[str, _MemoryFile] = {}
        self.capacity = capacity

    @property
    def used(self) -> int:
        """The bytes every file holds together."""
        return sum(len(file.data) for file in self._files.values())

    def _free(self, freed: int = 0) -> int | None:
        return None if self.capacity is None else max(0, self.capacity - self.used + freed)

    def append(self, name: str, data: bytes) -> None:
        """Append ``data`` to ``name``; on a full volume, append what fits and raise."""
        free = self._free()
        target = self._files.setdefault(name, _MemoryFile())
        if free is not None and len(data) > free:
            target.data += data[:free]
            raise OSError(errno.ENOSPC, f"no space left for {len(data)} bytes", name)
        target.data += data

    def replace(self, name: str, data: bytes) -> None:
        """Put a new file with ``data`` at ``name``, or raise and keep the old one."""
        existing = self._files.get(name)
        free = self._free(0 if existing is None else len(existing.data))
        if free is not None and len(data) > free:
            raise OSError(errno.ENOSPC, f"no space left for {len(data)} bytes", name)
        replacement = _MemoryFile()
        replacement.data += data
        self._files[name] = replacement

    def truncate(self, name: str, size: int) -> None:
        """Cut ``name`` to ``size`` bytes."""
        found = self._files.get(name)
        if found is None:
            raise FileNotFoundError(name)
        del found.data[size:]

    def remove(self, name: str) -> None:
        """Delete ``name``."""
        self._files.pop(name, None)

    def names(self, directory: str) -> list[str]:
        """File names directly in ``directory``."""
        prefix = f"{directory}/"
        return sorted(
            name[len(prefix) :]
            for name in self._files
            if name.startswith(prefix) and "/" not in name[len(prefix) :]
        )

    def source(self, name: str) -> ByteSource:
        """A source over ``name``."""
        return _MemorySource(self._files, name)
