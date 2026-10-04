"""Serialization and atomic I/O for typed VibeSys project state."""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ValidationError

from vs_project.errors import ProjectStateError, StateModelNotFoundError

if TYPE_CHECKING:
    from collections.abc import Iterator


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
        filesystem.replace(temporary_path, path)
        filesystem.sync_directory(path.parent)
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
    """Read a JSON object from a metadata path."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProjectStateError.metadata_file_missing(path) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectStateError.metadata_read_failed(path, exc) from exc
    if not isinstance(raw, dict):
        raise ProjectStateError.metadata_not_object(path)
    return raw


def _load_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Load and strictly validate a required metadata model."""
    try:
        content = path.read_text(encoding="utf-8")
        return model_type.model_validate_json(content, strict=True)
    except FileNotFoundError as exc:
        raise ProjectStateError.metadata_file_missing(path) from exc
    except (OSError, UnicodeError) as exc:
        raise ProjectStateError.metadata_read_failed(path, exc) from exc
    except ValidationError as exc:
        raise ProjectStateError.invalid_metadata(path, _validation_message(exc)) from exc


def _load_state_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    """Load and strictly validate a required operational-state model."""
    try:
        content = path.read_text(encoding="utf-8")
        return model_type.model_validate_json(content, strict=True)
    except FileNotFoundError as exc:
        raise StateModelNotFoundError.missing(path) from exc
    except (OSError, UnicodeError) as exc:
        raise ProjectStateError.state_read_failed(path, exc) from exc
    except ValidationError as exc:
        raise ProjectStateError.invalid_state_model(path, _validation_message(exc)) from exc


def _validation_message(error: ValidationError) -> str:
    """Render stable validation details without echoing input values."""
    failures: list[str] = []
    for detail in error.errors(include_url=False, include_context=False, include_input=False):
        location = ".".join(str(part) for part in detail["loc"]) or "metadata"
        failures.append(f"{location}: {detail['msg']}")
    return "; ".join(failures)
