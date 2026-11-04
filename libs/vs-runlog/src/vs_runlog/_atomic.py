"""Durable atomic publication of this store's protobuf files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from vs_proto.api import Keyframe, validate


class FileSync(Protocol):
    """Persist a descriptor's preceding writes; raise on an uncertain or failed flush."""

    def __call__(self, descriptor: int, /) -> None:
        """Return only after the file is durable; never take ownership of the descriptor."""
        ...


class OsFileSync:
    """The operating system's fsync, including its failure semantics."""

    def __call__(self, descriptor: int, /) -> None:
        """Flush a file or directory to stable storage."""
        os.fsync(descriptor)


class FakeFileSync:
    """Record successful flushes on an ephemeral filesystem without OS durability waits."""

    def __init__(self) -> None:
        """Start with no observed flushes."""
        self.calls = 0

    def __call__(self, descriptor: int, /) -> None:
        """Validate the borrowed descriptor and acknowledge its pending writes."""
        os.fstat(descriptor)
        self.calls += 1


_FILE_SYNC = OsFileSync()


def sync_directory(path: Path, *, sync: FileSync = _FILE_SYNC) -> None:
    """Persist directory entries after publication."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        sync(descriptor)
    finally:
        os.close(descriptor)


def publish(
    path: Path, payload: bytes, *, immutable: bool = False, sync: FileSync = _FILE_SYNC
) -> None:
    """Publish complete bytes; immutable names are idempotent or fail on conflict."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            sync(stream.fileno())
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    message = f"immutable keyframe conflicts with {path}"
                    raise ValueError(message) from None
        else:
            Path(temporary).replace(path)
        sync_directory(path.parent, sync=sync)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_keyframe(directory: Path, keyframe: Keyframe, *, sync: FileSync = _FILE_SYNC) -> None:
    """Create an immutable checkpoint, atomically and durably."""
    validate(keyframe)
    directory.mkdir(exist_ok=True)
    sync_directory(directory.parent, sync=sync)
    publish(
        directory / f"{keyframe.through_seq}.pb", keyframe.to_binary(), immutable=True, sync=sync
    )
