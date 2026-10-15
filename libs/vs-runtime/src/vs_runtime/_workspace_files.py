"""Files in an agent's workspace: what the agent wrote, read only as validated models.

The agent owns its workspace: every file there may hold anything, and any file or directory
may become a symlink out of it at any moment. This module is the one place the runtime reads
such a file or writes one for the agent, always through ``_workspace_tree``'s walk from a
descriptor of the workspace root, which refuses symlinks and ``..``. A read is bounded in
size, parsed by pydantic's depth-bounded JSON parser and validated strictly; it returns the
model or a typed ``WorkspaceFileProblem`` naming the file and the offending key path, never
a half-read value. Nothing read here is trusted: a result the runtime itself produced lives
in a runtime-owned store outside the workspace, and no trust decision reads the workspace.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from pydantic import BaseModel, JsonValue, TypeAdapter

from vs_decode.api import Unreadable, decode_json
from vs_runtime._workspace_tree import (
    EntryKind,
    WorkspacePathError,
    WorkspaceTree,
    open_workspace_tree,
    relative_parts,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
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
    data = _read(workspace, relative, max_bytes)
    if isinstance(data, WorkspaceFileProblem):
        return data
    decoded = decode_json(data, model, source=relative, max_bytes=max_bytes)
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
    data = _read(workspace, relative, max_bytes)
    if isinstance(data, WorkspaceFileProblem):
        return data
    decoded = decode_json(data, _JSON_VALUE, source=relative, max_bytes=max_bytes)
    return _problem(relative, decoded) if isinstance(decoded, Unreadable) else decoded


def _read(workspace: Path, relative: str, max_bytes: int) -> bytes | WorkspaceFileProblem:
    """At most ``max_bytes + 1`` bytes of the agent's regular file, or why there are none.

    A directory or a FIFO there is not readable rather than missing, and never blocks.
    """
    try:
        with open_workspace_tree(workspace) as tree:
            return tree.read_bytes(relative, max_bytes=max_bytes)
    except WorkspacePathError as error:
        return WorkspaceFileProblem(
            WorkspaceFileProblemKind.ESCAPES, relative, f"escapes the workspace: {error}"
        )
    except FileNotFoundError:
        return WorkspaceFileProblem(WorkspaceFileProblemKind.MISSING, relative, "does not exist")
    except OSError as error:
        reason = error.strerror or type(error).__name__
        return WorkspaceFileProblem(WorkspaceFileProblemKind.NOT_READABLE, relative, reason)


def _problem(relative: str, unreadable: Unreadable) -> WorkspaceFileProblem:
    return WorkspaceFileProblem(
        WorkspaceFileProblemKind(unreadable.kind.value), relative, unreadable.details()
    )


def write_for_agent(workspace: Path, relative: str, contents: bytes) -> None:
    """Atomically write ``contents`` at ``relative`` for the agent to read.

    Raises ``WorkspacePathError`` (a ``ValueError``) when the destination is absolute,
    holds ``..`` or passes through a symlink.
    """
    with open_workspace_tree(workspace) as tree:
        tree.write_bytes(relative, contents)


def file_fingerprint(workspace: Path, relative: str) -> str | None:
    """A SHA-256 of the regular file at ``relative``, or ``None`` if there is none.

    The file is hashed in fixed-size chunks, so its size costs time, not memory. A path
    that escapes ``workspace`` or passes through a symlink has no fingerprint.
    """
    digest = hashlib.sha256()
    try:
        with open_workspace_tree(workspace) as tree, tree.open_file(relative) as stream:
            while chunk := stream.read(_FINGERPRINT_CHUNK_BYTES):
                digest.update(chunk)
    except (OSError, WorkspacePathError):
        return None
    return digest.hexdigest()


def entry_kind(workspace: Path, relative: str) -> EntryKind | None:
    """What ``relative`` names without following a symlink; ``None`` if nothing is there.

    A path that escapes ``workspace`` or passes through a symlink names nothing in it.
    """
    try:
        with open_workspace_tree(workspace) as tree:
            return tree.kind(relative)
    except WorkspacePathError:
        return None


def hash_inputs(
    workspace: Path,
    relative_paths: Iterable[str],
    *,
    max_files: int,
    max_bytes: int,
) -> str:
    """The SHA-256 of the names and bytes of the files under ``relative_paths``.

    A directory contributes every regular file below it. Raises ``ValueError`` for an input
    that is missing, a symlink or below one, outside the workspace, or past the file and
    byte limits.
    """
    digest = hashlib.sha256()
    total_files = 0
    total_bytes = 0
    with open_workspace_tree(workspace) as tree:
        for relative in sorted(relative_paths):
            kind = _input_kind(tree, relative)
            digest.update(relative.encode())
            digest.update(b"\0dir\0" if kind is EntryKind.DIRECTORY else b"\0file\0")
            entries = (
                _files_below(tree, relative)
                if kind is EntryKind.DIRECTORY
                else (PurePosixPath(*relative_parts(relative)).as_posix(),)
            )
            for entry in entries:
                total_files += 1
                if total_files > max_files:
                    raise ValueError(_over_limit(max_files, max_bytes))
                digest.update(entry.encode())
                digest.update(b"\0")
                hashed = _hash_file(tree, entry, digest.update, max_bytes - total_bytes)
                if hashed is None:
                    raise ValueError(_over_limit(max_files, max_bytes))
                total_bytes += hashed
                digest.update(b"\0")
    return digest.hexdigest()


def _input_kind(tree: WorkspaceTree, relative: str) -> EntryKind:
    try:
        kind = tree.kind(relative)
    except WorkspacePathError as error:
        message = f"validation input escapes workspace: {error}"
        raise ValueError(message) from error
    if kind is None:
        message = f"validation input does not exist: {relative}"
        raise ValueError(message)
    if kind is EntryKind.SYMLINK:
        message = f"validation input must not be a symlink: {relative}"
        raise ValueError(message)
    return kind


def _files_below(tree: WorkspaceTree, relative: str) -> tuple[str, ...]:
    try:
        return tree.files_below(relative)
    except WorkspacePathError as error:
        message = f"validation input must not be a symlink: {error}"
        raise ValueError(message) from error


def _over_limit(max_files: int, max_bytes: int) -> str:
    return (
        f"validation inputs exceed the {max_files}-file/"
        f"{max_bytes // (1024 * 1024)}-MiB reuse-hash limit"
    )


def _hash_file(
    tree: WorkspaceTree, relative: str, update: Callable[[bytes], None], budget: int
) -> int | None:
    """Feed the regular file at ``relative`` to ``update``; its size, or ``None`` past ``budget``.

    The size is counted from the bytes read off the open file, not from an earlier
    ``stat``, so a file that grows after it is listed cannot pass the limit, and at most
    ``budget + 1`` bytes are read. Raises ``ValueError`` when ``relative`` cannot be opened
    or is not a regular file once opened (a FIFO would block the read).
    """
    try:
        with tree.open_file(relative) as stream:
            read = 0
            while chunk := stream.read(min(_FINGERPRINT_CHUNK_BYTES, budget - read + 1)):
                read += len(chunk)
                if read > budget:
                    return None
                update(chunk)
    except (OSError, WorkspacePathError) as error:
        message = f"validation input is not a readable regular file: {relative}"
        raise ValueError(message) from error
    return read


__all__ = [
    "MAX_AGENT_DOCUMENT_BYTES",
    "WorkspaceFileProblem",
    "WorkspaceFileProblemKind",
    "entry_kind",
    "file_fingerprint",
    "hash_inputs",
    "read_agent_json",
    "read_agent_model",
    "write_for_agent",
]
