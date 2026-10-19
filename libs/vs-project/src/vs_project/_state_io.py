"""Serialization and atomic I/O for typed VibeSys project state."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, TypeAdapter

from vs_decode.api import Unreadable, UnreadableKind, ValidationOptions, decode_json, read_json
from vs_project._framework_writes import FRAMEWORK_WRITES
from vs_project.errors import (
    ProjectStateError,
    StateDocumentDamagedError,
    StateModelNotFoundError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

STATE_DOCUMENT_MAX_BYTES = 256 * 1024 * 1024
"""The largest state document read; far past any the framework writes."""


def _serialize_state_model(model: BaseModel) -> bytes:
    """Return canonical JSON bytes for a typed state model."""
    try:
        content = json.dumps(
            model.model_dump(mode="json", round_trip=True),
            allow_nan=False,
            indent=2,
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ProjectStateError.state_serialization_failed() from exc
    return f"{content}\n".encode()


def _serialize_json_object(value: dict[str, object], *, subject: str) -> bytes:
    """Return canonical JSON bytes for a mapping."""
    try:
        content = json.dumps(value, allow_nan=False, indent=2, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ProjectStateError.json_serialization_failed(subject) from exc
    return f"{content}\n".encode()


def _atomic_write_model(path: Path, model: BaseModel) -> None:
    """Serialize and atomically replace a typed state model."""
    _atomic_write_bytes(path, _serialize_state_model(model))


class AtomicWriteStream(Protocol):
    """Writable binary stream used for staging an atomic publication."""

    def write(self, contents: bytes, /) -> int | None:
        """Accept bytes, returning the number written."""
        ...

    def flush(self) -> None:
        """Flush buffered bytes to the underlying file."""
        ...

    def fileno(self) -> int:
        """Return the local descriptor used for durability."""
        ...

    @property
    def closed(self) -> bool:
        """Whether the stream is closed."""
        ...


class AtomicWriteEffects(Protocol):
    """Filesystem operations used to durably replace a single file."""

    def temporary(
        self, destination: Path
    ) -> AbstractContextManager[tuple[Path, AtomicWriteStream]]:
        """Create a private temporary file beside the destination."""
        ...

    def sync_file(self, stream: AtomicWriteStream) -> None:
        """Persist the flushed stream before publishing its name."""
        ...

    def replace(self, temporary: Path, destination: Path) -> None:
        """Atomically publish a complete temporary file."""
        ...

    def sync_directory(self, directory: Path) -> None:
        """Persist the directory entry after replacement."""
        ...

    def remove_temporary(self, temporary: Path) -> None:
        """Remove staging residue; a published temporary is already absent."""
        ...


class StoreDurabilityEffects(AtomicWriteEffects, Protocol):
    """Atomic publication plus synchronizing a file this process did not just write."""

    def sync_existing_file(self, path: Path) -> None:
        """Persist the current bytes of an already published file."""
        ...


class LocalAtomicWriteEffects:
    """Local filesystem implementation of atomic replacement effects."""

    @contextmanager
    def temporary(self, destination: Path) -> Iterator[tuple[Path, AtomicWriteStream]]:
        """Own a private staging stream on the destination filesystem."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=destination.parent,
            prefix=f".{destination.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            yield Path(stream.name), stream

    def sync_file(self, stream: AtomicWriteStream) -> None:
        """Persist the complete staging file."""
        os.fsync(stream.fileno())

    def sync_existing_file(self, path: Path) -> None:
        """Persist a published file, refusing to follow a symlink at ``path``."""
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def replace(self, temporary: Path, destination: Path) -> None:
        """Atomically publish the staging file."""
        temporary.replace(destination)

    def sync_directory(self, directory: Path) -> None:
        """Persist replacement of the destination directory entry."""
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def remove_temporary(self, temporary: Path) -> None:
        """Clean up both successful and interrupted staging files."""
        temporary.unlink(missing_ok=True)


def sync_directory_chain(
    directory: Path, durable_root: Path, *, effects: AtomicWriteEffects | None = None
) -> None:
    """Persist namespace links through an existing durable root, inclusive.

    The caller owns this storage subtree. Its unchanged outer ancestors need
    only traversal permission and are outside the durability boundary.
    """
    directory.relative_to(durable_root)
    filesystem = effects if effects is not None else LocalAtomicWriteEffects()
    current = directory
    while True:
        filesystem.sync_directory(current)
        if current == durable_root:
            return
        current = current.parent


def atomic_write_bytes(
    path: Path, contents: bytes, *, effects: AtomicWriteEffects | None = None
) -> None:
    """Durably replace bytes using a private file, fsync, and atomic rename.

    Readers observe complete old or new bytes, including if writing fails.
    An error after replacement can leave the new contents published. The
    destination directory must permit replacement, and callers must not use
    this operation for files whose existing inode must remain granted.
    """
    filesystem = effects if effects is not None else LocalAtomicWriteEffects()
    temporary_path: Path | None = None
    try:
        with filesystem.temporary(path) as (temporary_path, stream):
            offset = 0
            while offset < len(contents):
                written = stream.write(contents[offset:])
                if written is None or written <= 0:
                    raise ProjectStateError.incomplete_write(path)
                offset += written
            stream.flush()
            filesystem.sync_file(stream)
        with FRAMEWORK_WRITES.publishing(path, contents):
            filesystem.replace(temporary_path, path)
        filesystem.sync_directory(path.parent)
    finally:
        if temporary_path is not None:
            filesystem.remove_temporary(temporary_path)


def exclusive_write_bytes(path: Path, contents: bytes) -> bool:
    """Durably create ``path`` with ``contents``, unless something already exists there.

    The bytes are written and synced under a private name, then linked into place: the
    kernel's link refuses an existing name, so of two writers racing for one name exactly
    one creates it, and a reader sees the complete bytes or no file. Returns whether this
    call created the file; the caller syncs the directory chain it owns.
    """
    filesystem = LocalAtomicWriteEffects()
    temporary_path: Path | None = None
    try:
        with filesystem.temporary(path) as (temporary_path, stream):
            stream.write(contents)
            stream.flush()
            filesystem.sync_file(stream)
        try:
            with FRAMEWORK_WRITES.creating(path, contents):
                os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary_path is not None:
            filesystem.remove_temporary(temporary_path)


def _atomic_write_bytes(path: Path, contents: bytes) -> None:
    """Replace owned state bytes through the shared durability mechanism."""
    atomic_write_bytes(path, contents)


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace UTF-8 text through the same durability mechanism as bytes."""
    atomic_write_bytes(path, content.encode("utf-8"))


def _read_json_object(path: Path) -> dict[str, object]:
    """Read a JSON object from a metadata path.

    Bad UTF-8 (a ``UnicodeDecodeError``), truncation and nesting past the interpreter's
    recursion limit (a ``RecursionError`` from ``json``) are each a
    ``ProjectStateError``, so a caller that skips damaged metadata skips all of it.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectStateError.metadata_file_missing(path) from exc
    except (OSError, ValueError, RecursionError) as exc:
        raise ProjectStateError.metadata_read_failed(path, exc) from exc
    if not isinstance(raw, dict):
        raise ProjectStateError.metadata_not_object(path)
    return raw


def _load_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Load and strictly validate a required metadata model."""
    decoded = read_json(
        path,
        model_type,
        max_bytes=STATE_DOCUMENT_MAX_BYTES,
        options=ValidationOptions(strict=True),
    )
    if not isinstance(decoded, Unreadable):
        return decoded
    match decoded.kind:
        case UnreadableKind.MISSING:
            raise ProjectStateError.metadata_file_missing(path)
        case UnreadableKind.NOT_READABLE:
            raise ProjectStateError.metadata_read_failed(path, OSError(decoded.details()))
        case _:
            raise ProjectStateError.invalid_metadata(path, decoded.details("metadata"))


def _load_state_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Load and strictly validate a required operational-state model."""
    try:
        content = path.read_bytes()
    except FileNotFoundError as exc:
        raise StateModelNotFoundError.missing(path) from exc
    except OSError as exc:
        raise ProjectStateError.state_read_failed(path, exc) from exc
    return decode_state_document(model_type, content, source=path)


def decode_state_document[ModelT: BaseModel](
    model_type: type[ModelT],
    data: bytes | str,
    *,
    source: Path | str,
    context: dict[str, Any] | None = None,
    versioned_by: tuple[str, int | range] | None = None,
) -> ModelT:
    """Strictly decode persisted bytes as ``model_type``: the one path every reader uses.

    Every decode failure (malformed or truncated JSON, bad UTF-8, schema
    violation, a version this release does not read) raises
    ``StateDocumentDamagedError`` naming ``source`` and the failing fields,
    never echoing the document. ``versioned_by=(field, expected)`` makes a
    document whose integer ``field`` differs from ``expected`` report both
    versions instead of a schema error. ``expected`` may be a range of versions, for a model
    that still reads what older releases wrote; the newest is the one reported.
    """
    if versioned_by is not None:
        field, expected = versioned_by
        readable = range(expected, expected + 1) if isinstance(expected, int) else expected
        found = _declared_version(data, field)
        if found is not None and found not in readable:
            raise StateDocumentDamagedError.unsupported_version(source, found, readable[-1])
    decoded = decode_json(
        data,
        model_type,
        source=str(source),
        max_bytes=STATE_DOCUMENT_MAX_BYTES,
        options=ValidationOptions(strict=True, context=context),
    )
    if isinstance(decoded, Unreadable):
        raise StateDocumentDamagedError.invalid(source, decoded.details("metadata"))
    return decoded


_JSON_OBJECT = TypeAdapter(dict[str, Any])


def _declared_version(data: bytes | str, field: str) -> int | None:
    """The integer version a JSON object declares, or None when it declares none."""
    document = decode_json(
        data, _JSON_OBJECT, source="state document", max_bytes=STATE_DOCUMENT_MAX_BYTES
    )
    if isinstance(document, Unreadable):
        # The model decode that follows reports the damage.
        return None
    value = document.get(field)
    return value if isinstance(value, int) and not isinstance(value, bool) else None
