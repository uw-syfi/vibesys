"""Where a store keeps its files: a directory on this host, or memory.

Both writer and reader reach the bytes only through a :class:`Volume`; the reader sees
them as :class:`~vs_sim.api.ByteSource` values, so it never assumes local files. Names are
relative, ``/``-separated, and built only by the store from validated stream ids.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from vs_sim.api import LocalFileSource, SourceWatch, check_read

if TYPE_CHECKING:
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

    def names(self, directory: str) -> list[str]:
        """The file names directly in ``directory``; empty when it does not exist."""
        ...

    def source(self, name: str) -> ByteSource:
        """A fresh source over ``name``; reading a missing one raises ``FileNotFoundError``."""
        ...

    def external(self, path: str) -> ByteSource:
        """A fresh source over a file the store does not write (an adopted log)."""
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

    def names(self, directory: str) -> list[str]:
        """File names in ``directory``."""
        path = self._root / directory
        if not path.is_dir():
            return []
        return sorted(entry.name for entry in path.iterdir() if entry.is_file())

    def source(self, name: str) -> ByteSource:
        """A :class:`~vs_sim.api.LocalFileSource` over ``name``."""
        return LocalFileSource(self._root / name)

    def external(self, path: str) -> ByteSource:
        """A :class:`~vs_sim.api.LocalFileSource` over ``path``."""
        return LocalFileSource(Path(path))


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
    """Files in memory, with the same visibility rules as :class:`DirectoryVolume`."""

    def __init__(self) -> None:
        """Start empty."""
        self._files: dict[str, _MemoryFile] = {}

    def append(self, name: str, data: bytes) -> None:
        """Append ``data`` to ``name``."""
        self._files.setdefault(name, _MemoryFile()).data += data

    def replace(self, name: str, data: bytes) -> None:
        """Put a new file with ``data`` at ``name``."""
        replacement = _MemoryFile()
        replacement.data += data
        self._files[name] = replacement

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

    def external(self, path: str) -> ByteSource:
        """A source over the memory file ``path`` (a test appends to it with :meth:`append`)."""
        return _MemorySource(self._files, path)
