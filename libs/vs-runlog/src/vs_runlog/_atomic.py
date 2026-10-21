"""Durable atomic publication of this store's protobuf files."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from vs_proto.api import Keyframe, validate


def sync_directory(path: Path) -> None:
    """Persist directory entries after publication."""
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish(path: Path, payload: bytes, *, immutable: bool = False) -> None:
    """Publish complete bytes; immutable names are idempotent or fail on conflict."""
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if immutable:
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    message = f"immutable keyframe conflicts with {path}"
                    raise ValueError(message) from None
        else:
            Path(temporary).replace(path)
        sync_directory(path.parent)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_keyframe(directory: Path, keyframe: Keyframe) -> None:
    """Create an immutable checkpoint, atomically and durably."""
    validate(keyframe)
    directory.mkdir(exist_ok=True)
    sync_directory(directory.parent)
    publish(directory / f"{keyframe.through_seq}.pb", keyframe.to_binary(), immutable=True)
