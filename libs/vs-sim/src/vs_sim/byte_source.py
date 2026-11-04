"""Read a growing run of bytes wherever it lives: a local file, a container's, a cluster's.

A :class:`ByteSource` answers two questions, how long the bytes are now and what lies at
an offset, so a reader can show a tail and then follow what is appended without knowing
where the bytes are kept. :class:`LocalFileSource` reads a file on this host; sources for
files in a container or on a cluster live in the libraries that own those transports and
pass the same contract suite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path


class SourceChange(StrEnum):
    """How a source stopped being the append-only bytes a reader has been reading."""

    SHRANK = "shrank"
    """It is shorter than it was seen to be: truncated or rewritten in place."""
    REPLACED = "replaced"
    """Its location now holds other bytes (a new file), or nothing."""


class ByteSourceChangedError(Exception):
    """The source is no longer the bytes it was: what was read before may not be there.

    A reader that got this starts over from a fresh source or reports the log as ended;
    reading on would silently mix two different logs.
    """

    def __init__(self, change: SourceChange, where: str) -> None:
        """Say how the source at ``where`` changed."""
        super().__init__(f"{where} {change}")
        self.change = change
        self.where = where


class ByteSource(Protocol):
    """Append-only bytes that may still grow, read by offset.

    Contract:

    - :meth:`size` is the number of bytes there are now; it never decreases.
    - :meth:`read` returns the bytes at ``offset``, at most ``max_bytes`` of them and at
      least one when any lie there; at or past the end it returns ``b""``. Reads at any
      offsets of a source that only grew reassemble its bytes.
    - Once a source has been seen, a shrink or a replacement raises
      :class:`ByteSourceChangedError` from both methods, from then on; it is never re-read
      silently as if it were the same bytes.
    - A negative ``offset`` or a ``max_bytes`` below one raises ``ValueError``.
    """

    def size(self) -> int:
        """How many bytes there are now."""
        ...

    def read(self, offset: int, max_bytes: int) -> bytes:
        """The bytes from ``offset``, at most ``max_bytes`` of them; ``b""`` at or past the end."""
        ...


@dataclass
class SourceWatch:
    """What a reader has seen of one source, to tell growth from a change.

    Implementations share it so they report changes the same way: each observation gives
    the source's identity (what it is, such as a file's device and inode) and its size.
    """

    where: str
    identity: object = None
    seen: int = 0
    """The most bytes the source was seen to hold."""
    change: SourceChange | None = None

    def observe(self, identity: object, size: int) -> None:
        """Record an observation; raise once the source shrank or was replaced."""
        if self.change is None:
            if self.identity is not None and identity != self.identity:
                self.change = SourceChange.REPLACED
            elif size < self.seen:
                self.change = SourceChange.SHRANK
        if self.change is not None:
            raise ByteSourceChangedError(self.change, self.where)
        self.identity = identity
        self.seen = size

    def lost(self) -> None:
        """Record that the source is gone; raise when it had been seen."""
        if self.identity is not None or self.change is not None:
            self.change = self.change or SourceChange.REPLACED
            raise ByteSourceChangedError(self.change, self.where)


def check_read(offset: int, max_bytes: int) -> None:
    """Reject a read no source can answer."""
    if offset < 0:
        message = f"a read offset must not be negative, got {offset}"
        raise ValueError(message)
    if max_bytes < 1:
        message = f"a read must ask for at least one byte, got {max_bytes}"
        raise ValueError(message)


class LocalFileSource:
    """A file on this host, read without holding it open between calls.

    A file that has never been seen and does not exist raises ``FileNotFoundError``, so a
    reader can wait for it to appear; once seen, its removal is a replacement.
    """

    def __init__(self, path: Path) -> None:
        """Read the file at ``path``."""
        self._path = path
        self._watch = SourceWatch(str(path))

    def size(self) -> int:
        """The file's size now."""
        try:
            status = self._path.stat()
        except FileNotFoundError:
            self._watch.lost()
            raise
        self._watch.observe((status.st_dev, status.st_ino), status.st_size)
        return status.st_size

    def read(self, offset: int, max_bytes: int) -> bytes:
        """The bytes at ``offset`` in the file, at most ``max_bytes`` of them."""
        check_read(offset, max_bytes)
        try:
            descriptor = os.open(self._path, os.O_RDONLY)
        except FileNotFoundError:
            self._watch.lost()
            raise
        try:
            status = os.fstat(descriptor)
            self._watch.observe((status.st_dev, status.st_ino), status.st_size)
            if offset >= status.st_size:
                return b""
            return os.pread(descriptor, min(max_bytes, status.st_size - offset), offset)
        finally:
            os.close(descriptor)
