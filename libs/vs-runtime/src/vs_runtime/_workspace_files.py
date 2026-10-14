"""Files in an agent's workspace: what the agent wrote, read only as validated models.

The agent owns its workspace: every file there may hold anything, and any of them may be a
symlink out of it. This module is the one place the runtime reads such a file or writes one
for the agent. A read is contained in the workspace, bounded in size, parsed by pydantic's
depth-bounded JSON parser and validated strictly; it returns the model or a typed
``WorkspaceFileProblem`` naming the file and the offending key path, never a half-read
value. Nothing read here is trusted: a result the runtime itself produced lives in a
runtime-owned store outside the workspace, and no trust decision reads the workspace.
"""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, JsonValue, TypeAdapter

from vs_decode.api import Unreadable, read_json
from vs_project.api import atomic_write_bytes

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

MAX_AGENT_DOCUMENT_BYTES = 1024 * 1024
"""The largest agent-written JSON document the runtime reads."""

_FINGERPRINT_CHUNK_BYTES = 1024 * 1024

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class WorkspaceFileProblemKind(StrEnum):
    """Why an agent-written file could not become a model.

    ``ESCAPES`` is this module's own; every other member carries the value of the
    ``vs_decode`` ``UnreadableKind`` it reports.
    """

    ESCAPES = "escapes"
    MISSING = "missing"
    NOT_READABLE = "not_readable"
    TOO_LARGE = "too_large"
    NOT_JSON = "not_json"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class WorkspaceFileProblem:
    """An agent-written file that is not a valid document, with where it went wrong."""

    kind: WorkspaceFileProblemKind
    relative_path: str
    detail: str
    """The failing key path and reason, without echoing the file's contents."""


def contained_path(workspace: Path, relative: str) -> Path | None:
    """The resolved path of ``relative`` inside ``workspace``, or ``None`` if it escapes."""
    root = workspace.resolve()
    path = (workspace / relative).resolve()
    return path if path.is_relative_to(root) else None


def read_agent_model[ModelT: BaseModel](
    workspace: Path,
    relative: str,
    model: type[ModelT],
    *,
    max_bytes: int = MAX_AGENT_DOCUMENT_BYTES,
) -> ModelT | WorkspaceFileProblem:
    """Read the agent's JSON file at ``relative`` as ``model``, or say why it is not one.

    ``model`` must forbid unknown keys at every depth (``vs_decode`` refuses it otherwise).
    """
    path = _contained_regular_file(workspace, relative)
    if isinstance(path, WorkspaceFileProblem):
        return path
    decoded = read_json(path, model, max_bytes=max_bytes)
    return _problem(relative, decoded) if isinstance(decoded, Unreadable) else decoded


def read_agent_json(
    workspace: Path,
    relative: str,
    *,
    max_bytes: int = MAX_AGENT_DOCUMENT_BYTES,
) -> JsonValue | WorkspaceFileProblem:
    """Read the agent's JSON file at ``relative`` as a plain JSON value, or say why not.

    For a format whose caller validates the shape itself to give its own messages.
    """
    path = _contained_regular_file(workspace, relative)
    if isinstance(path, WorkspaceFileProblem):
        return path
    decoded = read_json(path, _JSON_VALUE, max_bytes=max_bytes)
    return _problem(relative, decoded) if isinstance(decoded, Unreadable) else decoded


def _contained_regular_file(workspace: Path, relative: str) -> Path | WorkspaceFileProblem:
    """The contained path of ``relative``; anything there but a regular file is refused.

    A directory or a FIFO is not readable rather than missing, so the agent is told the
    file it wrote is wrong instead of being told it wrote nothing (opening a FIFO would
    also block the host).
    """
    path = contained_path(workspace, relative)
    if path is None:
        return WorkspaceFileProblem(
            WorkspaceFileProblemKind.ESCAPES, relative, "escapes the workspace"
        )
    if path.exists() and not path.is_file():
        return WorkspaceFileProblem(
            WorkspaceFileProblemKind.NOT_READABLE, relative, "is not a regular file"
        )
    return path


def _problem(relative: str, unreadable: Unreadable) -> WorkspaceFileProblem:
    return WorkspaceFileProblem(
        WorkspaceFileProblemKind(unreadable.kind.value), relative, unreadable.details()
    )


def write_for_agent(workspace: Path, relative: str, contents: bytes) -> None:
    """Atomically write ``contents`` at ``relative`` for the agent to read.

    Raises ``ValueError`` when the destination escapes the workspace.
    """
    path = contained_path(workspace, relative)
    if path is None:
        message = f"{relative} escapes the workspace"
        raise ValueError(message)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, contents)


def file_fingerprint(workspace: Path, relative: str) -> str | None:
    """A SHA-256 of the regular file at ``relative``, or ``None`` if there is none.

    The file is hashed in fixed-size chunks, so its size costs time, not memory. A path
    that resolves outside ``workspace`` (through a symlink the agent planted, say), or
    whose last component becomes a symlink before it is opened, has no fingerprint.
    """
    path = contained_path(workspace, relative)
    if path is None:
        return None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        return None
    with os.fdopen(descriptor, "rb") as stream:
        digest = hashlib.sha256()
        while chunk := stream.read(_FINGERPRINT_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def hash_inputs(
    workspace: Path,
    relative_paths: Iterable[str],
    *,
    max_files: int,
    max_bytes: int,
) -> str:
    """The SHA-256 of the names and bytes of the files under ``relative_paths``.

    A directory contributes every regular file below it. Raises ``ValueError`` for an input
    that is missing, a symlink, outside the workspace, or past the file and byte limits.
    """
    digest = hashlib.sha256()
    root = workspace.resolve()
    total_files = 0
    total_bytes = 0
    for relative in sorted(relative_paths):
        unresolved = workspace / relative
        if unresolved.is_symlink():
            message = f"validation input must not be a symlink: {relative}"
            raise ValueError(message)
        path = unresolved.resolve()
        if not path.is_relative_to(root):
            message = f"validation input escapes workspace: {relative}"
            raise ValueError(message)
        if not path.exists():
            message = f"validation input does not exist: {relative}"
            raise ValueError(message)
        digest.update(relative.encode())
        digest.update(b"\0dir\0" if path.is_dir() else b"\0file\0")
        entries = [path]
        if path.is_dir():
            entries = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
        for entry in entries:
            if entry.is_symlink():
                message = f"validation input must not be a symlink: {relative}"
                raise ValueError(message)
            total_files += 1
            total_bytes += entry.stat().st_size
            if total_files > max_files or total_bytes > max_bytes:
                message = (
                    f"validation inputs exceed the {max_files}-file/"
                    f"{max_bytes // (1024 * 1024)}-MiB reuse-hash limit"
                )
                raise ValueError(message)
            digest.update(entry.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(entry.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


__all__ = [
    "MAX_AGENT_DOCUMENT_BYTES",
    "WorkspaceFileProblem",
    "WorkspaceFileProblemKind",
    "contained_path",
    "file_fingerprint",
    "hash_inputs",
    "read_agent_json",
    "read_agent_model",
    "write_for_agent",
]
