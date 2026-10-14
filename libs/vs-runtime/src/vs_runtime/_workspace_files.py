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
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from vs_project.api import atomic_write_bytes

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

MAX_AGENT_DOCUMENT_BYTES = 1024 * 1024
"""The largest agent-written JSON document the runtime reads."""

_JSON_VALUE: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class WorkspaceFileProblemKind(StrEnum):
    """Why an agent-written file could not become a model."""

    ESCAPES = "escapes"
    MISSING = "missing"
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
    """Read the agent's JSON file at ``relative`` as ``model``, or say why it is not one."""
    content = _read_contained(workspace, relative, max_bytes)
    if isinstance(content, WorkspaceFileProblem):
        return content
    try:
        return model.model_validate_json(content)
    except ValidationError as error:
        return _problem(relative, error)


def read_agent_json(
    workspace: Path,
    relative: str,
    *,
    max_bytes: int = MAX_AGENT_DOCUMENT_BYTES,
) -> JsonValue | WorkspaceFileProblem:
    """Read the agent's JSON file at ``relative`` as a plain JSON value, or say why not.

    For a format whose caller validates the shape itself to give its own messages.
    """
    content = _read_contained(workspace, relative, max_bytes)
    if isinstance(content, WorkspaceFileProblem):
        return content
    try:
        return _JSON_VALUE.validate_json(content)
    except ValidationError as error:
        return _problem(relative, error)


def _read_contained(workspace: Path, relative: str, max_bytes: int) -> bytes | WorkspaceFileProblem:
    path = contained_path(workspace, relative)
    if path is None:
        return WorkspaceFileProblem(
            WorkspaceFileProblemKind.ESCAPES, relative, "escapes the workspace"
        )
    if not path.is_file():
        return WorkspaceFileProblem(WorkspaceFileProblemKind.MISSING, relative, "does not exist")
    try:
        with path.open("rb") as stream:
            content = stream.read(max_bytes + 1)
    except OSError as error:
        return WorkspaceFileProblem(WorkspaceFileProblemKind.MISSING, relative, str(error))
    if len(content) > max_bytes:
        return WorkspaceFileProblem(
            WorkspaceFileProblemKind.TOO_LARGE, relative, f"is larger than {max_bytes} bytes"
        )
    return content


def _problem(relative: str, error: ValidationError) -> WorkspaceFileProblem:
    details = error.errors(include_url=False, include_context=False, include_input=False)
    if any(detail["type"] == "json_invalid" for detail in details):
        return WorkspaceFileProblem(WorkspaceFileProblemKind.NOT_JSON, relative, details[0]["msg"])
    reasons = "; ".join(
        f"{'.'.join(str(part) for part in detail['loc']) or '<document>'}: {detail['msg']}"
        for detail in details
    )
    return WorkspaceFileProblem(WorkspaceFileProblemKind.INVALID, relative, reasons)


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
    "hash_inputs",
    "read_agent_json",
    "read_agent_model",
    "write_for_agent",
]
