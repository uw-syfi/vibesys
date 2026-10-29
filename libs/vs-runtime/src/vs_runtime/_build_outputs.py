"""A candidate build's declared outputs: their paths, their verification and their reuse.

An evaluator package names the files a candidate's build must produce
(``candidate_outputs``). After the confined build runs in a fresh checkout of the measured
commit, :func:`verify_outputs` reads each declared file through a ``WorkspaceTree`` (a
symlink or ``..`` planted by the build is refused), and the runtime records the verified
bytes in a ``BuildStore`` outside every workspace, keyed by the commit and the build. Check
and measure run in their own fresh checkouts; :meth:`BuildStore.materialize` places the
recorded outputs there at their declared paths, so a commit is built once and every later
stage of it, in this measurement or a later one, reuses that build.

The bytes are candidate output, never trusted: the store only guarantees that what a stage
loads is what the commit's build produced.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, Field

from vs_project.api.workspace_tree import (
    EntryKind,
    NotRegularFileError,
    WorkspacePathError,
    open_workspace_tree,
    relative_parts,
)

if TYPE_CHECKING:
    from pathlib import Path

    from vs_project.api import StateNamespace

MAX_OUTPUT_BYTES = 512 * 1024 * 1024
"""The largest single build output the runtime records."""

OUTPUTS_ENVIRONMENT_VARIABLE = "VIBESYS_CANDIDATE_OUTPUTS"
"""Names the verified outputs for check and measure: workspace-relative paths, ``:``-joined."""

_RESERVED_FIRST_COMPONENTS = frozenset({".git", ".vibesys"})


def check_candidate_output_path(path: str) -> None:
    """Refuse a declared output that is not a normalized workspace-relative file path."""
    try:
        parts = relative_parts(path)
    except WorkspacePathError as error:
        message = f"candidate output {path!r} must be a normalized workspace-relative path"
        raise ValueError(message) from error
    if not parts or "/".join(parts) != path or path.endswith("/"):
        message = f"candidate output {path!r} must be a normalized workspace-relative path"
        raise ValueError(message)
    if parts[0] in _RESERVED_FIRST_COMPONENTS:
        message = f"candidate output {path!r} lies in framework-owned {parts[0]}/"
        raise ValueError(message)
    if ":" in path:
        message = f"candidate output {path!r} must not contain ':'"
        raise ValueError(message)


class VerifiedOutput(BaseModel):
    """One declared output as the build left it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)


class MissingOutputError(ValueError):
    """The build finished without one of the outputs its evaluator requires."""

    def __init__(self, path: str, reason: str) -> None:
        """Name the output and why it does not count."""
        super().__init__(f"the build did not produce required output {path!r}: {reason}")
        self.path = path


def _read_output(root: Path, path: str) -> bytes:
    with open_workspace_tree(root) as tree:
        try:
            kind = tree.kind(path)
        except WorkspacePathError as error:
            raise MissingOutputError(path, "its path crosses a symlink") from error
        if kind is None:
            raise MissingOutputError(path, "no file is there")
        if kind is not EntryKind.FILE:
            raise MissingOutputError(path, f"it is a {kind.value}, not a regular file")
        try:
            contents = tree.read_bytes(path, max_bytes=MAX_OUTPUT_BYTES)
        except (NotRegularFileError, WorkspacePathError) as error:
            raise MissingOutputError(path, "it is not a regular file") from error
    if len(contents) > MAX_OUTPUT_BYTES:
        raise MissingOutputError(path, f"it is larger than {MAX_OUTPUT_BYTES} bytes")
    return contents


def verify_outputs(
    root: Path, outputs: tuple[str, ...]
) -> tuple[tuple[VerifiedOutput, bytes], ...]:
    """Read every declared output below ``root``; the first that does not count raises.

    Raises :class:`MissingOutputError` naming the output.
    """
    verified = []
    for path in outputs:
        contents = _read_output(root, path)
        verified.append(
            (
                VerifiedOutput(
                    path=path, sha256=hashlib.sha256(contents).hexdigest(), size=len(contents)
                ),
                contents,
            )
        )
    return tuple(verified)


def build_key(revision: str, command: str, outputs: tuple[str, ...]) -> str:
    """The identity of one build of one commit: the commit, the command and what it must make."""
    identity = json.dumps(
        {"revision": revision, "command": command, "outputs": list(outputs)}, sort_keys=True
    )
    return hashlib.sha256(identity.encode()).hexdigest()


class BuildRecord(BaseModel):
    """A build the runtime ran and verified."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: str = Field(min_length=1)
    outputs: tuple[VerifiedOutput, ...]


class BuildStore(Protocol):
    """Verified build outputs, kept by the runtime outside every workspace."""

    def find(self, key: str) -> BuildRecord | None:
        """The recorded build with this key, if the runtime ran and verified one."""
        ...

    def record(
        self, key: str, revision: str, outputs: tuple[tuple[VerifiedOutput, bytes], ...]
    ) -> BuildRecord:
        """Keep the verified outputs of one build."""
        ...

    def materialize(self, record: BuildRecord, root: Path) -> None:
        """Write the recorded outputs below ``root`` at their declared paths."""
        ...


class StateBuildStore:
    """Builds kept in a run's machine-local state namespace, content-addressed."""

    def __init__(self, namespace: StateNamespace) -> None:
        """Keep builds below ``namespace``, which lies outside every workspace."""
        self._namespace = namespace

    def find(self, key: str) -> BuildRecord | None:
        """The stored record, checked against its key."""
        record = self._namespace.load_optional(f"records/{key}.json", BuildRecord)
        return record if record is not None and record.key == key else None

    def record(
        self, key: str, revision: str, outputs: tuple[tuple[VerifiedOutput, bytes], ...]
    ) -> BuildRecord:
        """Store each output's bytes by digest, then the record that names them."""
        for output, contents in outputs:
            self._namespace.write_bytes(f"blobs/{output.sha256}", contents)
        record = BuildRecord(key=key, revision=revision, outputs=tuple(item for item, _ in outputs))
        self._namespace.save(f"records/{key}.json", record)
        return record

    def materialize(self, record: BuildRecord, root: Path) -> None:
        """Write each output below ``root``, refusing bytes that no longer match the record."""
        with open_workspace_tree(root) as tree:
            for output in record.outputs:
                contents = self._namespace.read_bytes(f"blobs/{output.sha256}")
                if contents is None or hashlib.sha256(contents).hexdigest() != output.sha256:
                    message = f"the recorded build output {output.path!r} is missing or changed"
                    raise ValueError(message)
                tree.write_bytes(output.path, contents)


class MemoryBuildStore:
    """Builds kept in memory: the store of a run whose state does not persist."""

    def __init__(self) -> None:
        """Start with no build recorded."""
        self._records: dict[str, BuildRecord] = {}
        self._blobs: dict[str, bytes] = {}

    def find(self, key: str) -> BuildRecord | None:
        """The record with this key, if one was recorded."""
        return self._records.get(key)

    def record(
        self, key: str, revision: str, outputs: tuple[tuple[VerifiedOutput, bytes], ...]
    ) -> BuildRecord:
        """Keep the outputs and their record."""
        for output, contents in outputs:
            self._blobs[output.sha256] = contents
        record = BuildRecord(key=key, revision=revision, outputs=tuple(item for item, _ in outputs))
        self._records[key] = record
        return record

    def materialize(self, record: BuildRecord, root: Path) -> None:
        """Write each recorded output below ``root``."""
        with open_workspace_tree(root) as tree:
            for output in record.outputs:
                tree.write_bytes(output.path, self._blobs[output.sha256])
