"""Typed filesystem boundary for project state below ``.vibesys/state``.

Git owns candidate source history. This module owns only portable completed-run
metadata, machine-local operational paths, and translation into validated Git
integration capabilities. It never invokes Git and has no knowledge of VibeSys
CLI arguments, agent providers, or evaluator implementations.
"""

# SLF001: these private values are shared only between cooperating types in this
# module. TRY003: these boundary errors deliberately embed the offending
# metadata path and value.
# ruff: noqa: SLF001, TRY003

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

import vs_project._paths as project_paths
from vs_loop_state import RoundRecord, parse_round_record
from vs_project._state_io import (
    _atomic_write_bytes,
    _atomic_write_model,
    _atomic_write_text,
    _load_model,
    _load_state_model,
    _read_json_object,
    _serialize_json_object,
    _serialize_state_model,
    _validate_portable_round,
    _validation_message,
    serialize_round,
)
from vs_project.errors import (
    ProjectStateError,
    RunSchemaMigrationRequiredError,
    StateModelNotFoundError,
)

if TYPE_CHECKING:
    from uuid import UUID

PROJECT_SCHEMA_VERSION: Literal[1] = 1
# Version 2 added the required run-environment recording. Version 3 adds the
# portable compute-resource request used to reproduce remote execution. Older
# recordings are rejected at load time and must be migrated explicitly.
RUN_SCHEMA_VERSION: Literal[3] = 3
_CONFIG_DIRECTORY_NAME = project_paths.CONFIGURATION_DIRECTORY_NAME
_STATE_DIRECTORY_PARTS = project_paths.STATE_DIRECTORY_PARTS
_STATE_DIRECTORY_PATH = project_paths.STATE_DIRECTORY_PATH
_STATE_DIRECTORY_POSIX = project_paths.STATE_DIRECTORY_POSIX
_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,127}$"
_DIGEST_PATTERN = r"^[0-9a-f]{64}$"
_GIT_OBJECT_ID_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_ROUND_FILE_PATTERN = re.compile(r"^(?P<round>0*[1-9][0-9]*)\.json$")
_STATE_HOME_ENV = "VIBESYS_STATE_HOME"
_LEGACY_WORKTREE_MIN_PARTS = 3
_EXCLUDED_NAMES = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "__pycache__",
        "agent.toml",
        "node_modules",
    }
)

Identifier = Annotated[str, Field(pattern=_IDENTIFIER_PATTERN)]
Sha256Digest = Annotated[str, Field(pattern=_DIGEST_PATTERN)]
GitObjectId = Annotated[str, Field(pattern=_GIT_OBJECT_ID_PATTERN)]
PortableText = Annotated[str, Field(min_length=1, max_length=256)]


def is_project_state_path(relative_path: Path | str) -> bool:
    """Return whether a safe project-relative path is owned by this package."""
    path = Path(relative_path)
    if path.is_absolute() or path == Path() or ".." in path.parts:
        raise ProjectStateError(f"Project path must be a safe relative path: {relative_path}")
    return any(
        path.parts[index : index + len(_STATE_DIRECTORY_PARTS)] == _STATE_DIRECTORY_PARTS
        for index in range(len(path.parts) - len(_STATE_DIRECTORY_PARTS) + 1)
    )


@dataclass(frozen=True)
class ProjectSandboxPaths:
    """Project-relative framework paths for a sandbox policy.

    A missing path is represented by ``None`` so callers can combine this
    capability with their own policy without interpreting the state layout.
    """

    read_only_path: Path | None
    hidden_path: Path | None


@dataclass(frozen=True, init=False)
class StateDocument:
    """Opaque immutable JSON state document owned by this package."""

    _project_relative_path: PurePosixPath
    _contents: bytes

    @classmethod
    def _create(cls, project_relative_path: PurePosixPath, contents: bytes) -> Self:
        """Construct a validated package-owned document."""
        _validate_project_state_path(project_relative_path)
        if not isinstance(contents, bytes):
            raise TypeError("state document contents must be bytes")
        try:
            payload = json.loads(contents)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("state document contents must be a JSON object") from exc
        if not isinstance(payload, dict):
            raise TypeError("state document contents must be a JSON object")
        document = object.__new__(cls)
        object.__setattr__(document, "_project_relative_path", project_relative_path)
        object.__setattr__(document, "_contents", contents)
        return document


@dataclass(frozen=True, init=False)
class StateTransition:
    """Opaque immutable replacement or deletion of one state document."""

    _project_relative_path: PurePosixPath
    _next_document: StateDocument | None

    @classmethod
    def _create(
        cls,
        project_relative_path: PurePosixPath,
        next_document: StateDocument | None,
    ) -> Self:
        """Construct a validated package-owned transition."""
        _validate_project_state_path(project_relative_path)
        if (
            next_document is not None
            and next_document._project_relative_path != project_relative_path
        ):
            raise ValueError("state transition document path must match its target path")
        transition = object.__new__(cls)
        object.__setattr__(transition, "_project_relative_path", project_relative_path)
        object.__setattr__(transition, "_next_document", next_document)
        return transition


@dataclass(frozen=True)
class StateFile:
    """One immutable file in a portable state snapshot.

    ``relative_path`` is relative to the opaque snapshot scope.
    """

    relative_path: PurePosixPath
    contents: bytes

    def __post_init__(self) -> None:
        """Reject unsafe paths and mutable or textual contents."""
        _validate_snapshot_relative_path(self.relative_path)
        if not isinstance(self.contents, bytes):
            raise TypeError("state snapshot file contents must be bytes")


@dataclass(frozen=True, init=False)
class StateSnapshot:
    """Deterministic, immutable selection of portable project-state files.

    The root is ``.vibesys/state``, one run directory, or one run-state namespace. File
    paths are relative to that root. The combined paths can never address
    machine-local state below ``.vibesys/state/local``.
    """

    _namespace_root: PurePosixPath
    files: tuple[StateFile, ...]

    @classmethod
    def _create(cls, namespace_root: PurePosixPath, files: tuple[StateFile, ...]) -> Self:
        """Construct a validated package-owned snapshot."""
        _validate_snapshot_root(namespace_root)
        if not isinstance(files, tuple):
            raise TypeError("state snapshot files must be an immutable tuple")
        if any(not isinstance(item, StateFile) for item in files):
            raise TypeError("state snapshot files must contain StateFile values")
        paths = tuple(item.relative_path for item in files)
        if paths != tuple(sorted(paths, key=PurePosixPath.as_posix)):
            raise ValueError("state snapshot files must be ordered by relative path")
        if len(paths) != len(set(paths)):
            raise ValueError("state snapshot files must have unique relative paths")
        for path in paths:
            combined = namespace_root / path
            if combined.parts[:3] == (*_STATE_DIRECTORY_PARTS, "local"):
                raise ValueError(
                    "portable state snapshots must not contain .vibesys/state/local files"
                )
        snapshot = object.__new__(cls)
        object.__setattr__(snapshot, "_namespace_root", namespace_root)
        object.__setattr__(snapshot, "files", files)
        return snapshot


@dataclass(frozen=True)
class GitSnapshotFile:
    """One validated portable-state file ready for a Git integration."""

    pathspec: str
    destination: Path
    contents: bytes


@dataclass(frozen=True)
class GitSnapshotPlan:
    """Opaque filesystem and pathspec capability for committing one snapshot."""

    scope_pathspec: str
    destination_root: Path
    files: tuple[GitSnapshotFile, ...]

    def contains_pathspec(self, pathspec: str) -> bool:
        """Return whether a Git-reported path belongs to this snapshot scope."""
        return pathspec == self.scope_pathspec or pathspec.startswith(f"{self.scope_pathspec}/")


@dataclass(frozen=True)
class ProjectGitIntegration:
    """Semantic Git capabilities for one project's framework-owned state.

    Git adapters may pass the returned pathspecs to Git and write the resolved
    destinations. They do not need to know or validate the underlying project
    layout.
    """

    _project_root: Path
    _run_id: str

    @property
    def local_exclude_pattern(self) -> str:
        """Return the repository-local ignore pattern for machine-local state."""
        return f"/{_STATE_DIRECTORY_PATH.as_posix()}/local/"

    @property
    def metadata_pathspec(self) -> str:
        """Return the pathspec selecting all framework-owned project state."""
        return _STATE_DIRECTORY_PATH.as_posix()

    @property
    def metadata_restore_exclusions(self) -> tuple[str, ...]:
        """Return pathspecs that preserve trusted VibeSys files during tree restores."""
        return (
            f":(exclude){_CONFIG_DIRECTORY_NAME}",
            f":(exclude){_CONFIG_DIRECTORY_NAME}/**",
        )

    @property
    def metadata_clean_exclusion(self) -> str:
        """Return the ignore expression that preserves trusted VibeSys files on clean."""
        return f"{_CONFIG_DIRECTORY_NAME}/"

    def validate_candidate_worktree(self, path: Path) -> Path:
        """Resolve a candidate worktree below this run's machine-local area."""
        store = ProjectState(self._project_root)
        worktrees_root = store._worktrees_dir(self._run_id)
        raw_destination = path.expanduser()
        if not raw_destination.is_absolute():
            raw_destination = self._project_root / raw_destination
        try:
            destination = _contained_without_symlinks(
                worktrees_root,
                raw_destination,
                kind="candidate worktree",
            )
        except ProjectStateError as exc:
            raise ValueError(f"candidate worktree must be below {worktrees_root}: {path}") from exc
        destination = destination.resolve()
        if destination == worktrees_root.resolve():
            raise ValueError(f"candidate worktree must be below {worktrees_root}: {path}")
        return destination

    def resolve_snapshot(self, snapshot: StateSnapshot) -> GitSnapshotPlan:
        """Resolve a validated portable snapshot into opaque Git capabilities."""
        ProjectState(self._project_root)
        namespace_root = snapshot._namespace_root
        parts = namespace_root.parts
        if parts != _STATE_DIRECTORY_PARTS and parts[3] != self._run_id:
            raise ValueError(f"state snapshot belongs to run {parts[3]!r}, not {self._run_id!r}")
        destination_root = _contained_without_symlinks(
            self._project_root,
            self._project_root.joinpath(*namespace_root.parts),
            kind="Git snapshot root",
        )
        files = tuple(
            GitSnapshotFile(
                pathspec=(namespace_root / state_file.relative_path).as_posix(),
                destination=_contained_without_symlinks(
                    destination_root,
                    destination_root.joinpath(*state_file.relative_path.parts),
                    kind="Git snapshot file",
                ),
                contents=state_file.contents,
            )
            for state_file in snapshot.files
        )
        return GitSnapshotPlan(
            scope_pathspec=namespace_root.as_posix(),
            destination_root=destination_root,
            files=files,
        )

    def resolve_replacement_snapshot(self, snapshot: StateSnapshot) -> GitSnapshotPlan:
        """Resolve an exact replacement of one namespace owned by this run."""
        namespace_root = snapshot._namespace_root
        if len(namespace_root.parts) != 5 or namespace_root.parts[3] != self._run_id:  # noqa: PLR2004
            raise ValueError(
                "framework state snapshot must select a dedicated namespace "
                f"for run {self._run_id!r}"
            )
        return self.resolve_snapshot(snapshot)


class StateNamespace:
    """Opaque, safe filesystem boundary for one run-state namespace.

    Instances are created through :attr:`vs_project.Project.state`. Callers address files only
    by namespace-relative portable paths and exchange validated Pydantic models.
    Machine-local namespaces support the same model operations but cannot be
    converted into portable snapshots.
    """

    __slots__ = (
        "_containment_root",
        "_kind",
        "_namespace_root",
        "_portable",
        "_project_root",
        "_root",
    )

    def __init__(
        self,
        *,
        project_root: Path,
        root: Path,
        portable: bool,
        containment_root: Path | None = None,
        namespace_root: PurePosixPath | None = None,
    ) -> None:
        """Bind one validated project-owned namespace root."""
        self._project_root = project_root
        self._root = root
        self._portable = portable
        self._kind = "portable" if portable else "local"
        self._containment_root = containment_root or project_root
        self._namespace_root = namespace_root or PurePosixPath(
            root.relative_to(project_root).as_posix()
        )
        _validate_project_state_path(self._namespace_root)
        self._validated_root()

    def load[ModelT: BaseModel](
        self,
        relative_path: str | PurePosixPath,
        model_type: type[ModelT],
    ) -> ModelT:
        """Load and strictly validate one required state model."""
        path = self._resolve_file(relative_path)
        return _load_state_model(path, model_type)

    def load_optional[ModelT: BaseModel](
        self,
        relative_path: str | PurePosixPath,
        model_type: type[ModelT],
    ) -> ModelT | None:
        """Return a valid state model, or ``None`` only when it is absent.

        Malformed JSON and model validation failures remain errors. They are
        never conflated with a missing optional checkpoint.
        """
        path = self._resolve_file(relative_path)
        try:
            return _load_state_model(path, model_type)
        except StateModelNotFoundError:
            return None

    def save(self, relative_path: str | PurePosixPath, model: BaseModel) -> None:
        """Atomically serialize one state model at a safe relative path."""
        self.apply(self.transition(relative_path, model))

    def slot[ModelT: BaseModel](
        self,
        relative_path: str | PurePosixPath,
        model_type: type[ModelT],
    ) -> StateSlot[ModelT]:
        """Bind one path and model schema as a reusable typed state slot."""
        return StateSlot(self, relative_path, model_type)

    def transition(
        self,
        relative_path: str | PurePosixPath,
        model: BaseModel | None,
    ) -> StateTransition:
        """Prepare an immutable replacement or deletion without applying it."""
        self._resolve_file(relative_path)
        project_relative_path = self._project_relative_path(relative_path)
        document = (
            None
            if model is None
            else StateDocument._create(project_relative_path, _serialize_state_model(model))
        )
        return StateTransition._create(project_relative_path, document)

    def apply(self, transition: StateTransition) -> None:
        """Atomically apply a transition prepared for this namespace."""
        self._validated_root()
        try:
            relative_path = transition._project_relative_path.relative_to(self._namespace_root)
        except ValueError as exc:
            raise ProjectStateError(
                f"State transition target is outside this namespace: "
                f"{transition._project_relative_path}"
            ) from exc
        path = self._resolve_file(relative_path)
        try:
            if transition._next_document is None:
                if path.exists() and not path.is_file():
                    raise ProjectStateError(f"VibeSys state path is not a file: {path}")
                path.unlink(missing_ok=True)
            else:
                _atomic_write_bytes(path, transition._next_document._contents)
        except OSError as exc:
            raise ProjectStateError(
                f"Could not apply VibeSys state transition at {path}: {exc}"
            ) from exc

    def delete(self, relative_path: str | PurePosixPath) -> bool:
        """Delete one state file, returning whether it existed."""
        path = self._resolve_file(relative_path)
        if not path.exists():
            return False
        if not path.is_file():
            raise ProjectStateError(f"VibeSys state path is not a file: {path}")
        try:
            path.unlink()
        except OSError as exc:
            raise ProjectStateError(
                f"Could not delete VibeSys state model at {path}: {exc}"
            ) from exc
        return True

    def snapshot(self) -> StateSnapshot:
        """Return an ordered immutable snapshot of this portable namespace."""
        if not self._portable:
            raise ProjectStateError("Machine-local VibeSys state namespaces cannot be snapshotted")
        root = self._validated_root()
        if not root.exists():
            return StateSnapshot._create(self._namespace_root, ())

        files: list[StateFile] = []
        try:
            paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
            for path in paths:
                relative = path.relative_to(root)
                if path.is_symlink():
                    raise ProjectStateError(
                        f"VibeSys {self._kind} state must not contain symlinks: {path}"
                    )
                if path.is_dir():
                    continue
                if not path.is_file():
                    raise ProjectStateError(
                        f"VibeSys {self._kind} state contains an unsupported file type: {path}"
                    )
                files.append(
                    StateFile(
                        relative_path=PurePosixPath(relative.as_posix()),
                        contents=path.read_bytes(),
                    )
                )
        except OSError as exc:
            raise ProjectStateError(
                f"Could not snapshot VibeSys {self._kind} state at {root}: {exc}"
            ) from exc
        return StateSnapshot._create(self._namespace_root, tuple(files))

    def agent_visible_path(self, relative_path: str | PurePosixPath | None = None) -> str:
        """Return a safe project-relative location for an agent-facing prompt.

        Filesystem reads and writes must still use this namespace's typed methods.
        """
        if not self._portable:
            raise ProjectStateError("Machine-local VibeSys state is not agent-visible")
        return self._project_relative_path(relative_path).as_posix()

    def equivalent_external_file(
        self,
        project_root: Path | str,
        relative_path: str | PurePosixPath,
    ) -> Path:
        """Resolve the equivalent state file inside another project worktree."""
        if not self._portable:
            raise ProjectStateError("Machine-local VibeSys state has no worktree equivalent")
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise ProjectStateError(f"Project root is not a directory: {root}")
        relative = self._project_relative_path(relative_path)
        return _contained_without_symlinks(
            root,
            root.joinpath(*relative.parts),
            kind="equivalent external state file",
        )

    def _project_relative_path(
        self,
        relative_path: str | PurePosixPath | None = None,
    ) -> PurePosixPath:
        """Return this namespace's validated project-relative location."""
        self._validated_root()
        result = self._namespace_root
        if relative_path is not None:
            result /= _validate_state_relative_path(relative_path)
        return result

    def external_directory(self, relative_directory: str | PurePosixPath | None = None) -> Path:
        """Materialize a safe directory for an external path-based API.

        Framework-owned model persistence should use :meth:`load` and
        :meth:`save`. This escape hatch is only for external libraries whose
        contracts require them to manage a directory tree directly.
        """
        root = self._validated_root()
        directory = (
            root
            if relative_directory is None
            else _contained_without_symlinks(
                root,
                root.joinpath(*_validate_state_relative_path(relative_directory).parts),
                kind=f"{self._kind} external state directory",
            )
        )
        try:
            if directory.exists() and not directory.is_dir():
                raise ProjectStateError(
                    f"VibeSys {self._kind} external state path is not a directory: {directory}"
                )
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectStateError(
                f"Could not create VibeSys {self._kind} external state directory {directory}: {exc}"
            ) from exc
        return directory

    def _validated_root(self) -> Path:
        root = _contained_without_symlinks(
            self._containment_root,
            self._root,
            kind=f"{self._kind} state namespace",
        )
        try:
            if root.exists() and not root.is_dir():
                raise ProjectStateError(
                    f"VibeSys {self._kind} state namespace is not a directory: {root}"
                )
        except OSError as exc:
            raise ProjectStateError(
                f"Could not validate VibeSys {self._kind} state namespace {root}: {exc}"
            ) from exc
        return root

    def _resolve_file(self, relative_path: str | PurePosixPath) -> Path:
        root = self._validated_root()
        relative = _validate_state_relative_path(relative_path)
        path = root.joinpath(*relative.parts)
        return _contained_without_symlinks(root, path, kind=f"{self._kind} state file")


class StateSlot[ModelT: BaseModel]:
    """One schema-bound state file within a namespace.

    Externally reconstructed transitions must pass through this boundary before
    they are applied. This ensures recovery code cannot restore a JSON object
    that violates the owning subsystem's model schema.
    """

    __slots__ = ("_model_type", "_namespace", "_relative_path")

    def __init__(
        self,
        namespace: StateNamespace,
        relative_path: str | PurePosixPath,
        model_type: type[ModelT],
    ) -> None:
        """Bind a validated namespace path to one Pydantic model type."""
        self._namespace = namespace
        self._relative_path = _validate_state_relative_path(relative_path)
        self._model_type = model_type

    def load_optional(self) -> ModelT | None:
        """Load this slot, returning ``None`` only when it is absent."""
        return self._namespace.load_optional(self._relative_path, self._model_type)

    def transition(self, model: ModelT | None) -> StateTransition:
        """Prepare an exact replacement or deletion for this slot."""
        return self._namespace.transition(self._relative_path, model)

    def save(self, model: ModelT | None) -> None:
        """Atomically save or clear this slot."""
        self.apply(self.transition(model))

    def serialize_transition(self, transition: StateTransition) -> bytes:
        """Serialize one validated transition without exposing its state path."""
        validated = self.validate_transition(transition)
        document = (
            None
            if validated._next_document is None
            else json.loads(validated._next_document._contents)
        )
        return _serialize_json_object(
            {
                "schema_version": 1,
                "document": document,
            },
            subject="state transition",
        )

    def snapshot_transition(self, transition: StateTransition) -> StateSnapshot:
        """Return the exact portable snapshot produced by one replacement.

        A ``StateSnapshot`` represents files that must exist, so deletion
        transitions cannot be expressed through this API. Callers that need
        namespace replacement semantics must use ``StateNamespace.snapshot``
        after applying the deletion instead.
        """
        validated = self.validate_transition(transition)
        if validated._next_document is None:
            raise ProjectStateError("Cannot create a state snapshot from a deletion transition")
        return StateSnapshot._create(
            namespace_root=self._namespace._namespace_root,
            files=(
                StateFile(
                    relative_path=self._relative_path,
                    contents=validated._next_document._contents,
                ),
            ),
        )

    def deserialize_transition(self, payload: bytes) -> StateTransition:
        """Parse and schema-validate a transition for exactly this slot."""
        if not isinstance(payload, bytes):
            raise TypeError("serialized state transition must be bytes")
        try:
            raw = json.loads(payload)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProjectStateError("Serialized state transition is not valid JSON") from exc
        if not isinstance(raw, dict):
            raise ProjectStateError("Serialized state transition must be a JSON object")
        if set(raw) != {"schema_version", "document"} or raw["schema_version"] != 1:
            raise ProjectStateError("Serialized state transition has an invalid schema")
        document = raw["document"]
        if document is None:
            return self.transition(None)
        if not isinstance(document, dict):
            raise ProjectStateError("Serialized state transition document must be an object")
        try:
            model = self._model_type.model_validate_json(
                json.dumps(document),
                strict=True,
            )
        except ValidationError as exc:
            raise ProjectStateError(
                f"Serialized state transition does not match the slot schema: {exc}"
            ) from exc
        return self.transition(model)

    def validate_transition(self, transition: StateTransition) -> StateTransition:
        """Validate a reconstructed transition's target and replacement schema."""
        expected_path = self._namespace._project_relative_path(self._relative_path)
        if transition._project_relative_path != expected_path:
            raise ProjectStateError(
                "State transition must target the typed slot: "
                f"expected {expected_path}, got "
                f"{transition._project_relative_path}"
            )
        if transition._next_document is not None:
            try:
                self._model_type.model_validate_json(
                    transition._next_document._contents,
                    strict=True,
                )
            except ValidationError as exc:
                raise ProjectStateError(
                    "State transition document does not match the typed slot schema at "
                    f"{expected_path}: {exc}"
                ) from exc
        return transition

    def apply(self, transition: StateTransition) -> None:
        """Validate and atomically apply a transition to this slot."""
        self._namespace.apply(self.validate_transition(transition))


class _CommittedManifest(BaseModel):
    """Strict base for versioned, portable metadata committed with source.

    Each concrete manifest declares its own ``schema_version`` literal so the
    project and run schemas can evolve independently.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ProjectManifest(_CommittedManifest):
    """Immutable identity and initial provenance of one project directory."""

    schema_version: Literal[1]
    project_id: Identifier
    created_at: AwareDatetime
    initial_input_fingerprint: Sha256Digest


class RunResourceRequest(BaseModel):
    """Portable compute resources required by one run environment.

    Operator-owned cluster profiles resolve this logical request to concrete
    infrastructure. Provider names, partitions, accounts, images, paths, and
    transient allocation identifiers deliberately do not belong here.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    nodes: Annotated[int, Field(gt=0)] = 1
    accelerators_per_node: Annotated[int, Field(gt=0)]
    accelerator_backend: Literal["cuda", "rocm", "trainium"]
    cpus_per_node: Annotated[int, Field(gt=0)] | None = None


class RunEnvironmentRecord(BaseModel):
    """Runtime environment a run executes in, recorded for faithful resume.

    ``name`` selects the environment; the remaining fields carry that
    environment's operator-selected options and stay ``None`` when they do not
    apply. Values a run derives from its own input (rather than from the
    operator) are deliberately absent: they are re-derived on every launch.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: Literal["local", "docker", "modal", "skypilot"]
    image: PortableText | None = None
    gpu: PortableText | None = None
    model_volume: PortableText | None = None
    app: PortableText | None = None
    resources: RunResourceRequest | None = None


class _BaseRunConfiguration(BaseModel):
    """Strict settings shared by every supported outer loop."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_environment: RunEnvironmentRecord
    model: PortableText | None = None
    agent_backend: PortableText
    agent_driver: PortableText | None = None
    cli_provider: PortableText | None = None
    cli_timeout: Annotated[int, Field(gt=0)] | None = None
    compute_backend: PortableText
    profiler: PortableText | None = None
    modality: PortableText | None = None
    default_reasoning_effort: PortableText | None = None
    outer_model: PortableText | None = None
    outer_reasoning_effort: PortableText | None = None
    inner_model: PortableText | None = None
    inner_reasoning_effort: PortableText | None = None


class AgentRunConfiguration(_BaseRunConfiguration):
    """Sanitized settings that define an agent-loop run."""

    outer_loop: Literal["agent"]
    inner_loop: PortableText
    interface: PortableText
    max_rounds: Annotated[int, Field(gt=0)]
    max_retries_per_round: Annotated[int, Field(gt=0)]
    judge_every: Annotated[int, Field(gt=0)]
    official_eval_every: Annotated[int, Field(gt=0)]
    memory_layout: PortableText
    operator_constraints: tuple[str, ...] = ()
    objectives: tuple[PortableText, ...] = ()


class PlainRunConfiguration(_BaseRunConfiguration):
    """Sanitized settings that define an issue-driven plain-loop run."""

    outer_loop: Literal["plain"]
    max_rounds: Annotated[int, Field(gt=0)]
    max_attempts_per_issue: Annotated[int, Field(gt=0)]
    max_issues_per_perf_eval: Annotated[int, Field(gt=0)]


class EvolveRunConfiguration(_BaseRunConfiguration):
    """Sanitized settings that define an evolutionary-search run."""

    outer_loop: Literal["evolve"]
    max_generations: Annotated[int, Field(gt=0)]
    children_per_generation: Annotated[int, Field(gt=0)]
    k_top_inspirations: Annotated[int, Field(ge=0)]
    k_random_inspirations: Annotated[int, Field(ge=0)]
    selection_temperature: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    seed: int | None = None
    search_policy: Literal["vibesys", "openevolve"] | None = None
    openevolve_population_size: Annotated[int, Field(gt=0)] | None = None
    openevolve_archive_size: Annotated[int, Field(gt=0)] | None = None
    openevolve_num_islands: Annotated[int, Field(gt=0)] | None = None
    openevolve_migration_interval: Annotated[int, Field(gt=0)] | None = None
    openevolve_migration_rate: Annotated[float, Field(ge=0, le=1)] | None = None
    frontier_bias: Annotated[float, Field(ge=0, le=1)]
    bootstrap_max_attempts: Annotated[int, Field(gt=0)]
    keep_deployments: bool
    max_parallelism: Annotated[int, Field(gt=0)]
    objectives: tuple[PortableText, ...] = ()

    @model_validator(mode="after")
    def _validate_search_policy_settings(self) -> Self:
        openevolve_values = (
            self.openevolve_population_size,
            self.openevolve_archive_size,
            self.openevolve_num_islands,
            self.openevolve_migration_interval,
            self.openevolve_migration_rate,
        )
        if self.search_policy == "vibesys" and any(
            value is not None for value in openevolve_values
        ):
            raise ValueError("OpenEvolve settings require search_policy='openevolve'")
        return self


RunConfiguration = Annotated[
    AgentRunConfiguration | PlainRunConfiguration | EvolveRunConfiguration,
    Field(discriminator="outer_loop"),
]


class RunManifest(_CommittedManifest):
    """Immutable identity and starting provenance of one optimization run."""

    schema_version: Literal[3]
    run_id: Identifier
    project_id: Identifier
    task_name: Identifier | None = None
    display_name: PortableText
    created_at: AwareDatetime
    input_fingerprint: Sha256Digest
    trusted_input_baseline: GitObjectId
    branch: PortableText
    vibesys_version: PortableText
    configuration: RunConfiguration


class _Digest(Protocol):
    def update(self, data: bytes, /) -> object:
        """Add bytes to the digest state."""


def generate_run_id(
    display_name: str,
    *,
    now: datetime | None = None,
    unique: UUID | None = None,
) -> str:
    """Return a sortable, path-safe run ID.

    ``now`` and ``unique`` are injectable so callers can reproduce IDs in tests.
    The display name is cosmetic: unsafe characters are normalized and an empty
    result becomes ``run``.
    """
    timestamp = now or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ProjectStateError("Run ID timestamp must include a timezone")
    timestamp = timestamp.astimezone(UTC)
    suffix = (unique or uuid.uuid4()).hex[:8]
    normalized = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or "run"
    slug = slug[:64].rstrip("-") or "run"
    return f"{timestamp:%Y%m%d-%H%M%S}-{suffix}-{slug}"


class ProjectState:
    """Internal persistence implementation exposed through ``Project.state``."""

    def __init__(self, project_root: Path | str) -> None:
        """Bind the store to an existing project directory."""
        root = Path(project_root).resolve()
        if not root.is_dir():
            raise ProjectStateError(f"Project root is not a directory: {root}")
        self.project_root = root
        self._config_dir = root / _CONFIG_DIRECTORY_NAME
        self._metadata_dir = root / _STATE_DIRECTORY_PATH
        self._project_manifest_path = self._metadata_dir / "project.json"
        self._metadata_gitignore_path = self._metadata_dir / ".gitignore"
        self._legacy_local_dir = self._metadata_dir / "local"
        self._state_home = _state_home()
        _prepare_state_home(self._state_home)
        self._local_dir = _external_project_state_directory(self._state_home, root)
        self._current_run_path = self._local_dir / "current-run"
        self._validate_storage_roots()
        self._migrate_legacy_local_state()

    @classmethod
    def is_project_root(cls, path: Path | str) -> bool:
        """Return whether *path* is an initialized VibeSys project root."""
        root = Path(path).expanduser()
        try:
            if not root.is_dir() or root.is_symlink():
                return False
            config = root / _CONFIG_DIRECTORY_NAME
            metadata = root / _STATE_DIRECTORY_PATH
            manifest = metadata / "project.json"
            if (
                config.is_symlink()
                or metadata.is_symlink()
                or manifest.is_symlink()
                or not manifest.is_file()
            ):
                return False
            _load_model(manifest, ProjectManifest)
        except (OSError, ProjectStateError):
            return False
        return True

    @classmethod
    def find_projects(cls, collection: Path | str) -> tuple[Path, ...]:
        """Return initialized projects directly below an existing collection."""
        root = Path(collection).expanduser().resolve()
        if not root.is_dir():
            return ()
        try:
            children = tuple(root.iterdir())
        except OSError as exc:
            raise ProjectStateError(f"Could not inspect project collection {root}: {exc}") from exc
        return tuple(
            sorted(
                (child.resolve() for child in children if cls.is_project_root(child)),
                key=Path.as_posix,
            )
        )

    @classmethod
    def log_directory_for(cls, project_root: Path | str, run_id: str) -> Path:
        """Return a run log destination before the project root is materialized."""
        root = Path(project_root).expanduser().resolve()
        if root.exists() and not root.is_dir():
            raise ProjectStateError(f"Project root is not a directory: {root}")
        normalized = _validate_run_id(run_id)
        state_home = _state_home()
        _prepare_state_home(state_home)
        local_dir = _external_project_state_directory(state_home, root)
        legacy_local_dir = root / _STATE_DIRECTORY_PATH / "local"
        _validate_storage_root(local_dir, state_home, name="local metadata")
        _migrate_legacy_local_directory(legacy_local_dir, local_dir)
        return _contained_without_symlinks(
            local_dir,
            local_dir / "runs" / normalized / "logs",
            kind="run log directory",
        )

    def sandbox_paths(self) -> ProjectSandboxPaths:
        """Return existing framework paths for an application sandbox policy."""
        self._validate_storage_roots()
        return ProjectSandboxPaths(
            read_only_path=(Path(_CONFIG_DIRECTORY_NAME) if self._config_dir.exists() else None),
            hidden_path=(
                _STATE_DIRECTORY_PATH / "local" if self._legacy_local_dir.exists() else None
            ),
        )

    def log_directory(self, run_id: str) -> Path:
        """Return the machine-local log directory for one run."""
        self._validate_storage_roots()
        return self.log_directory_for(self.project_root, run_id)

    def model_cache_directory(self, name: str) -> Path:
        """Return a named machine-local model cache directory."""
        self._validate_storage_roots()
        cache_root = _contained_without_symlinks(
            self._local_dir,
            self._local_dir / "cache",
            kind="model cache root",
        )
        return _contained_state_dir(cache_root, name, kind="model cache")

    def candidate_worktree_directory(self, run_id: str, candidate_id: str) -> Path:
        """Return the exact Git worktree directory for one run candidate."""
        candidate_root = _contained_state_dir(
            self._worktrees_dir(run_id),
            candidate_id,
            kind="candidate worktree",
        )
        return _contained_state_dir(candidate_root, "workspace", kind="candidate workspace")

    def portable_run_export(self, run_id: str) -> StateSnapshot:
        """Return all portable documents for one run as an immutable snapshot."""
        self.load_run(run_id)
        return _snapshot_directory(
            project_root=self.project_root, root=self._contained_run_dir(run_id)
        )

    def git_integration(self, run_id: str) -> ProjectGitIntegration:
        """Return opaque Git integration capabilities for one run."""
        return ProjectGitIntegration(
            _project_root=self.project_root,
            _run_id=_validate_run_id(run_id),
        )

    def input_fingerprint(self) -> str:
        """Hash the portable project input, excluding metadata, secrets, and caches."""
        digest = hashlib.sha256(b"vs-project-input-v1\0")
        paths = sorted(
            self.project_root.rglob("*"),
            key=lambda path: path.relative_to(self.project_root).as_posix(),
        )
        for path in paths:
            relative = path.relative_to(self.project_root)
            if _is_excluded(relative):
                continue
            _update_fingerprint(digest, path, relative)
        return digest.hexdigest()

    def create_project(
        self,
        display_name: str,
        *,
        now: datetime | None = None,
    ) -> ProjectManifest:
        """Create the project manifest, or return the existing manifest unchanged."""
        self._validate_storage_roots()
        if self._project_manifest_path.exists():
            manifest = self.load_project()
            self._ensure_local_gitignore()
            return manifest
        fingerprint = self.input_fingerprint()
        manifest = ProjectManifest(
            schema_version=PROJECT_SCHEMA_VERSION,
            project_id=_project_id(display_name, fingerprint),
            created_at=_aware_now(now),
            initial_input_fingerprint=fingerprint,
        )
        _atomic_write_model(self._project_manifest_path, manifest)
        self._ensure_local_gitignore()
        return manifest

    def load_project(self) -> ProjectManifest:
        """Load the project manifest with path-specific validation errors."""
        self._validate_storage_roots()
        return _load_model(self._project_manifest_path, ProjectManifest)

    def new_run_manifest(  # noqa: PLR0913
        self,
        display_name: str,
        *,
        branch: str,
        vibesys_version: str,
        configuration: RunConfiguration,
        trusted_input_baseline: GitObjectId,
        task_name: str | None = None,
        run_id: str | None = None,
        now: datetime | None = None,
        unique: UUID | None = None,
    ) -> RunManifest:
        """Build, but do not persist, a run manifest for the current project tree."""
        project = self.load_project()
        created_at = _aware_now(now)
        return RunManifest(
            schema_version=RUN_SCHEMA_VERSION,
            run_id=(
                _validate_run_id(run_id)
                if run_id is not None
                else generate_run_id(display_name, now=created_at, unique=unique)
            ),
            project_id=project.project_id,
            task_name=task_name,
            display_name=display_name,
            created_at=created_at,
            input_fingerprint=self.input_fingerprint(),
            trusted_input_baseline=trusted_input_baseline,
            branch=branch,
            vibesys_version=vibesys_version,
            configuration=configuration,
        )

    def create_run(self, manifest: RunManifest, *, make_current: bool = True) -> None:
        """Persist a new run manifest and initialize its local operational paths."""
        self._validate_storage_roots()
        project = self.load_project()
        if manifest.project_id != project.project_id:
            raise ProjectStateError(
                f"Run {manifest.run_id!r} belongs to project {manifest.project_id!r}, "
                f"not {project.project_id!r}"
            )
        path = self._run_manifest_path(manifest.run_id)
        if path.exists():
            existing = self.load_run(manifest.run_id)
            if existing != manifest:
                raise ProjectStateError(f"Run metadata already exists with different data: {path}")
        else:
            _atomic_write_model(path, manifest)
        self.log_directory(manifest.run_id).mkdir(parents=True, exist_ok=True)
        if make_current:
            self.set_current_run(manifest.run_id)

    def initialization_snapshot(self, run_id: str) -> StateSnapshot:
        """Snapshot the metadata required to initialize one project run in Git."""
        self.load_project()
        self.load_run(run_id)
        return _snapshot_selected_files(
            project_root=self.project_root,
            root=self._metadata_dir,
            paths=(
                self._metadata_gitignore_path,
                self._project_manifest_path,
                self._run_manifest_path(run_id),
            ),
        )

    def load_run(self, run_id: str) -> RunManifest:
        """Load one run manifest, rejecting recordings from an older schema."""
        path = self._run_manifest_path(run_id)
        _require_current_run_schema(path, run_id)
        return _load_model(path, RunManifest)

    def migrate_run_environment(
        self,
        run_id: str,
        run_environment: RunEnvironmentRecord,
    ) -> RunManifest:
        """Migrate a version 1 or 2 run to the current environment schema.

        Run schema version 1 never recorded the runtime environment, so the
        operator supplies the environment the run actually used. Version 2
        already recorded it, so the supplied value must match before the
        optional portable resource request is added. The migration is one-way.
        """
        self._validate_storage_roots()
        path = self._run_manifest_path(run_id)
        raw = _read_json_object(path)
        recorded_version = raw.get("schema_version")
        if recorded_version == RUN_SCHEMA_VERSION:
            raise ProjectStateError(
                f"Run metadata at {path} is already at run schema version {RUN_SCHEMA_VERSION}"
            )
        if recorded_version not in {1, 2}:
            raise ProjectStateError(
                f"Run metadata at {path} records unsupported run schema version "
                f"{recorded_version!r}; only versions 1 and 2 can be migrated"
            )
        configuration = raw.get("configuration")
        if not isinstance(configuration, dict):
            raise ProjectStateError(f"Run metadata at {path} has no configuration object")
        if recorded_version == 1:
            if "run_environment" in configuration:
                raise ProjectStateError(
                    f"Run metadata at {path} already records a run environment; "
                    "its schema version is inconsistent with its contents"
                )
            migrated_environment = run_environment
        else:
            recorded_environment = configuration.get("run_environment")
            try:
                migrated_environment = RunEnvironmentRecord.model_validate(
                    recorded_environment, strict=True
                )
            except (TypeError, ValueError) as exc:
                raise ProjectStateError(
                    f"Run metadata at {path} has an invalid run environment: {exc}"
                ) from exc
            if migrated_environment != run_environment:
                raise ProjectStateError(
                    f"Run metadata at {path} records a different run environment; "
                    "supply the environment already recorded by version 2"
                )
        migrated = {
            **raw,
            "schema_version": RUN_SCHEMA_VERSION,
            "configuration": {
                **configuration,
                "run_environment": migrated_environment.model_dump(mode="json"),
            },
        }
        try:
            manifest = RunManifest.model_validate_json(json.dumps(migrated), strict=True)
        except (TypeError, ValueError) as exc:
            raise ProjectStateError(f"Could not migrate VibeSys metadata at {path}: {exc}") from exc
        _atomic_write_model(path, manifest)
        return manifest

    def run_manifest_snapshot(self, run_id: str) -> StateSnapshot:
        """Snapshot the current portable manifest for one run."""
        self.load_run(run_id)
        path = self._run_manifest_path(run_id)
        return _snapshot_selected_files(
            project_root=self.project_root,
            root=path.parent,
            paths=(path,),
        )

    def update_run_configuration(
        self,
        run_id: str,
        configuration: RunConfiguration,
    ) -> None:
        """Replace a run's sanitized configuration while preserving its identity."""
        self._validate_storage_roots()
        manifest = self.load_run(run_id)
        if configuration.outer_loop != manifest.configuration.outer_loop:
            raise ProjectStateError(
                f"Run {manifest.run_id!r} uses outer loop "
                f"{manifest.configuration.outer_loop!r}, not {configuration.outer_loop!r}"
            )
        path = self._run_manifest_path(run_id)
        updated = manifest.model_copy(update={"configuration": configuration})
        if updated != manifest:
            _atomic_write_model(path, updated)

    def list_runs(self) -> list[RunManifest]:
        """Return all runs ordered by creation time, then run ID."""
        self._validate_storage_roots()
        runs_dir = self._metadata_dir / "runs"
        if not runs_dir.exists():
            return []
        manifests: list[RunManifest] = []
        for child in sorted(runs_dir.iterdir()):
            if not child.is_dir():
                raise ProjectStateError(f"Unexpected file in VibeSys runs directory: {child}")
            manifests.append(self.load_run(child.name))
        return sorted(manifests, key=lambda manifest: (manifest.created_at, manifest.run_id))

    def latest_run(self) -> RunManifest | None:
        """Return the most recently created run, if one exists."""
        runs = self.list_runs()
        return runs[-1] if runs else None

    def current_run_id(self) -> str | None:
        """Return the machine-local current run pointer, if it is set."""
        self._validate_storage_roots()
        if not self._current_run_path.exists():
            return None
        try:
            value = self._current_run_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ProjectStateError(
                f"Could not read current run pointer {self._current_run_path}: {exc}"
            ) from exc
        return _validate_run_id(value, source=self._current_run_path)

    def set_current_run(self, run_id: str | None) -> None:
        """Atomically update or clear the machine-local current run pointer."""
        self._validate_storage_roots()
        if run_id is None:
            self._current_run_path.unlink(missing_ok=True)
            return
        normalized = _validate_run_id(run_id)
        self.load_run(normalized)
        _atomic_write_text(self._current_run_path, f"{normalized}\n")

    def resolve_run(self, run_id: str | None = None) -> RunManifest:
        """Resolve an explicit run, otherwise current, otherwise latest."""
        if run_id is not None:
            return self.load_run(run_id)
        current = self.current_run_id()
        if current is not None:
            return self.load_run(current)
        latest = self.latest_run()
        if latest is None:
            raise ProjectStateError(f"No VibeSys runs exist under {self._metadata_dir}")
        return latest

    def save_round(self, run_id: str, record: RoundRecord) -> StateSnapshot:
        """Persist one completed round and return its exact portable snapshot."""
        self._validate_storage_roots()
        self.load_run(run_id)
        contents = serialize_round(record)
        completed = self.load_rounds(run_id)
        path = self._rounds_dir(run_id) / f"{record.round_number:04d}.json"
        if record.round_number <= len(completed):
            existing = completed[record.round_number - 1]
            if existing != record:
                raise ProjectStateError(
                    f"Completed round already exists with different data: {path}"
                )
            return self.completed_round_snapshot(run_id, record.round_number)
        next_round = len(completed) + 1
        if record.round_number != next_round:
            raise ProjectStateError(
                f"Completed rounds must be appended in order: expected round {next_round}, "
                f"got {record.round_number}"
            )
        _atomic_write_text(path, contents.decode("utf-8"))
        return self.completed_round_snapshot(run_id, record.round_number)

    def prepare_completed_round_snapshot(
        self,
        run_id: str,
        record: RoundRecord,
    ) -> StateSnapshot:
        """Build the canonical snapshot for a typed completed round without writing it."""
        self.load_run(run_id)
        _validate_portable_round(record, source=self.project_root)
        root = self._portable_state_dir(run_id, "agent")
        round_path = self._rounds_dir(run_id) / f"{record.round_number:04d}.json"
        return StateSnapshot._create(
            namespace_root=PurePosixPath(root.relative_to(self.project_root).as_posix()),
            files=(
                StateFile(
                    relative_path=PurePosixPath(round_path.relative_to(root).as_posix()),
                    contents=serialize_round(record),
                ),
            ),
        )

    def restore_completed_round(self, run_id: str, record: RoundRecord) -> StateSnapshot:
        """Restore one already-committed round from its typed canonical record."""
        self.load_run(run_id)
        _validate_portable_round(record, source=self.project_root)
        directory = self._rounds_dir(run_id)
        self._validate_round_restore_position(directory, record.round_number)
        path = directory / f"{record.round_number:04d}.json"
        _atomic_write_bytes(path, serialize_round(record))
        return self.completed_round_snapshot(run_id, record.round_number)

    def load_rounds(self, run_id: str) -> list[RoundRecord]:
        """Load completed rounds in numeric order."""
        self.load_run(run_id)
        directory = self._rounds_dir(run_id)
        if not directory.exists():
            return []
        numbered_paths: list[tuple[int, Path]] = []
        for path in directory.iterdir():
            match = _ROUND_FILE_PATTERN.fullmatch(path.name)
            if not path.is_file() or match is None:
                raise ProjectStateError(f"Unexpected completed-round entry: {path}")
            numbered_paths.append((int(match.group("round")), path))
        records: list[RoundRecord] = []
        for sequence_number, (file_number, path) in enumerate(sorted(numbered_paths), start=1):
            if file_number != sequence_number:
                raise ProjectStateError(
                    "Completed rounds must form a contiguous sequence starting at 1: "
                    f"expected round {sequence_number}, found {file_number} at {path}"
                )
            record = self._load_round(path)
            if record.round_number != file_number:
                raise ProjectStateError(
                    f"Round file {path} contains round {record.round_number}, "
                    f"expected {file_number}"
                )
            records.append(record)
        return records

    def completed_round_snapshot(self, run_id: str, round_number: int) -> StateSnapshot:
        """Snapshot one validated completed-round record."""
        if round_number < 1:
            raise ProjectStateError(f"Round number must be positive, got {round_number}")
        records = self.load_rounds(run_id)
        if round_number > len(records):
            raise ProjectStateError(
                f"Completed round {round_number} does not exist for run {run_id!r}"
            )
        root = self._portable_state_dir(run_id, "agent")
        path = self._rounds_dir(run_id) / f"{round_number:04d}.json"
        return _snapshot_selected_files(
            project_root=self.project_root,
            root=root,
            paths=(path,),
        )

    def _run_manifest_path(self, run_id: str) -> Path:
        """Return the committed manifest path for *run_id*."""
        return self._contained_run_dir(run_id) / "run.json"

    def _rounds_dir(self, run_id: str) -> Path:
        """Return the agent loop's committed completed-round directory."""
        return _contained_state_dir(
            self._portable_state_dir(run_id, "agent"),
            "rounds",
            kind="completed-round",
        )

    def _portable_state_dir(self, run_id: str, namespace: str) -> Path:
        """Return one loop or subsystem's portable state directory."""
        return _contained_state_dir(
            self._contained_run_dir(run_id),
            namespace,
            kind="portable",
        )

    def portable_namespace(self, run_id: str, namespace: str) -> StateNamespace:
        """Return the typed filesystem boundary for committed subsystem state."""
        self.load_run(run_id)
        return StateNamespace(
            project_root=self.project_root,
            root=self._portable_state_dir(run_id, namespace),
            portable=True,
        )

    def _local_state_dir(self, run_id: str, namespace: str) -> Path:
        """Return one loop or subsystem's machine-local state directory."""
        return _contained_state_dir(
            self._contained_local_run_dir(run_id),
            namespace,
            kind="local",
        )

    def local_namespace(self, run_id: str, namespace: str) -> StateNamespace:
        """Return the typed filesystem boundary for machine-local subsystem state."""
        self.load_run(run_id)
        return StateNamespace(
            project_root=self.project_root,
            root=self._local_state_dir(run_id, namespace),
            portable=False,
            containment_root=self._local_dir,
            namespace_root=(_STATE_DIRECTORY_POSIX / "local" / "runs" / run_id / namespace),
        )

    def _round_transaction_path(self, run_id: str) -> Path:
        """Return the machine-local round commit transaction path."""
        return self._contained_local_run_dir(run_id) / "round-transaction.json"

    def _worktrees_dir(self, run_id: str) -> Path:
        """Return the machine-local directory reserved for candidate worktrees."""
        normalized = _validate_run_id(run_id)
        legacy_run_dir = _contained_without_symlinks(
            self._legacy_local_dir,
            self._legacy_local_dir / "runs" / normalized,
            kind="candidate worktree run",
        )
        return _contained_state_dir(
            legacy_run_dir,
            "worktrees",
            kind="worktrees",
        )

    def _contained_run_dir(self, run_id: str) -> Path:
        self._validate_storage_roots()
        normalized = _validate_run_id(run_id)
        return _contained_without_symlinks(
            self._metadata_dir,
            self._metadata_dir / "runs" / normalized,
            kind="portable run",
        )

    def _contained_local_run_dir(self, run_id: str) -> Path:
        self._validate_storage_roots()
        normalized = _validate_run_id(run_id)
        return _contained_without_symlinks(
            self._local_dir,
            self._local_dir / "runs" / normalized,
            kind="local run",
        )

    def _validate_storage_roots(self) -> None:
        _validate_storage_root(self._config_dir, self.project_root, name="configuration")
        _validate_storage_root(self._metadata_dir, self._config_dir, name="metadata")
        _validate_storage_root(
            self._legacy_local_dir,
            self._metadata_dir,
            name="legacy local metadata",
        )
        _validate_storage_root(self._local_dir, self._state_home, name="local metadata")

    def _migrate_legacy_local_state(self) -> None:
        """Move legacy repository-local operational state to the user state home."""
        _migrate_legacy_local_directory(self._legacy_local_dir, self._local_dir)

    def _ensure_local_gitignore(self) -> None:
        self._validate_storage_roots()
        required = "/local/"
        try:
            existing = (
                self._metadata_gitignore_path.read_text(encoding="utf-8")
                if self._metadata_gitignore_path.exists()
                else ""
            )
        except OSError as exc:
            raise ProjectStateError(
                f"Could not read VibeSys ignore contract {self._metadata_gitignore_path}: {exc}"
            ) from exc
        if required in existing.splitlines():
            return
        separator = "" if not existing or existing.endswith("\n") else "\n"
        _atomic_write_text(self._metadata_gitignore_path, f"{existing}{separator}{required}\n")

    @staticmethod
    def _load_round(path: Path) -> RoundRecord:
        payload = _read_json_object(path)
        try:
            record = parse_round_record(payload)
        except ValidationError as exc:
            raise ProjectStateError(
                f"Invalid completed-round metadata at {path}: {_validation_message(exc)}"
            ) from exc
        _validate_portable_round(record, source=path)
        return record

    @classmethod
    def _validate_round_restore_position(cls, directory: Path, round_number: int) -> None:
        """Require valid predecessors while permitting repair of the target file."""
        if round_number < 1:
            raise ProjectStateError(f"Round number must be positive, got {round_number}")
        if not directory.exists():
            existing: dict[int, Path] = {}
        else:
            existing = {}
            for path in directory.iterdir():
                match = _ROUND_FILE_PATTERN.fullmatch(path.name)
                if not path.is_file() or path.is_symlink() or match is None:
                    raise ProjectStateError(f"Unexpected completed-round entry: {path}")
                file_number = int(match.group("round"))
                if file_number in existing:
                    raise ProjectStateError(
                        f"Duplicate completed-round number {file_number}: "
                        f"{existing[file_number]} and {path}"
                    )
                existing[file_number] = path

        unexpected_later = sorted(number for number in existing if number > round_number)
        if unexpected_later:
            raise ProjectStateError(
                f"Cannot restore round {round_number} before existing round {unexpected_later[0]}"
            )
        for predecessor in range(1, round_number):
            path = existing.get(predecessor)
            if path is None:
                raise ProjectStateError(
                    f"Cannot restore round {round_number} without completed round {predecessor}"
                )
            restored = cls._load_round(path)
            if restored.round_number != predecessor:
                raise ProjectStateError(
                    f"Round file {path} contains round {restored.round_number}, "
                    f"expected {predecessor}"
                )


def _aware_now(value: datetime | None) -> datetime:
    timestamp = value or datetime.now(UTC)
    if timestamp.tzinfo is None:
        raise ProjectStateError("Metadata timestamp must include a timezone")
    return timestamp.astimezone(UTC)


def _project_id(display_name: str, fingerprint: str) -> str:
    normalized = unicodedata.normalize("NFKD", display_name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or "project"
    slug = slug[:64].rstrip("-") or "project"
    return f"{slug}-{fingerprint[:12]}"


def _state_home() -> Path:
    """Resolve the operator-configurable root for machine-local VibeSys state."""
    configured = os.environ.get(_STATE_HOME_ENV)
    if configured is not None:
        if not configured.strip():
            raise ProjectStateError(f"{_STATE_HOME_ENV} must not be empty")
        root = Path(configured).expanduser()
        if not root.is_absolute():
            raise ProjectStateError(f"{_STATE_HOME_ENV} must be an absolute path: {configured}")
    else:
        root = Path.home() / ".vibesys"
    return root.resolve()


def _external_project_state_directory(state_home: Path, project_root: Path) -> Path:
    """Return a collision-resistant local directory for one canonical project path."""
    normalized = unicodedata.normalize("NFKD", project_root.name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-") or "project"
    slug = slug[:64].rstrip("-") or "project"
    digest = hashlib.sha256(project_root.as_posix().encode("utf-8")).hexdigest()[:12]
    destination = state_home / "projects" / f"{slug}-{digest}"
    if destination.resolve().is_relative_to(project_root.resolve()):
        raise ProjectStateError(
            f"{_STATE_HOME_ENV} must place machine-local state outside the project: {state_home}"
        )
    return destination


def _prepare_state_home(state_home: Path) -> None:
    """Create private user-state parents before any run metadata is written."""
    try:
        state_home.mkdir(parents=True, exist_ok=True, mode=0o700)
        (state_home / "projects").mkdir(exist_ok=True, mode=0o700)
    except OSError as exc:
        raise ProjectStateError(f"Could not create VibeSys state home {state_home}: {exc}") from exc


def _migrate_legacy_local_directory(source: Path, destination: Path) -> None:
    """Atomically relocate legacy local metadata while leaving worktrees in place."""
    if destination.exists() or not source.is_dir():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.migrating-", dir=destination.parent)
    )
    try:
        _validate_legacy_migration_tree(source)
        shutil.copytree(source, temporary, dirs_exist_ok=True)
        for run_worktrees in temporary.glob("runs/*/worktrees"):
            shutil.rmtree(run_worktrees)
        try:
            temporary.replace(destination)
        except OSError:
            if not destination.is_dir():
                raise
        _remove_migrated_legacy_entries(source)
    except OSError as exc:
        raise ProjectStateError(
            f"Could not migrate VibeSys local state from {source} to {destination}: {exc}"
        ) from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _remove_migrated_legacy_entries(source: Path) -> None:
    """Remove copied metadata from the legacy tree without touching worktrees."""
    for entry in tuple(source.iterdir()):
        if entry.name != "runs":
            _remove_path(entry)
            continue
        if entry.is_symlink() or not entry.is_dir():
            _remove_path(entry)
            continue
        _remove_migrated_run_entries(entry)
        if not any(entry.iterdir()):
            entry.rmdir()
    if not any(source.iterdir()):
        source.rmdir()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


def _remove_migrated_run_entries(runs_directory: Path) -> None:
    for run_dir in tuple(runs_directory.iterdir()):
        if not run_dir.is_dir() or run_dir.is_symlink():
            _remove_path(run_dir)
            continue
        for run_entry in tuple(run_dir.iterdir()):
            if run_entry.name != "worktrees":
                _remove_path(run_entry)
        if not any(run_dir.iterdir()):
            run_dir.rmdir()


def _validate_legacy_migration_tree(source: Path) -> None:
    """Reject legacy metadata aliases while allowing untouched worktree contents."""
    for path in source.rglob("*"):
        relative = path.relative_to(source)
        if (
            len(relative.parts) >= _LEGACY_WORKTREE_MIN_PARTS
            and relative.parts[0] == "runs"
            and relative.parts[2] == "worktrees"
        ):
            continue
        if path.is_symlink():
            raise ProjectStateError(f"VibeSys local metadata path must not be a symlink: {path}")


def _validate_run_id(run_id: str, *, source: Path | None = None) -> str:
    if re.fullmatch(_IDENTIFIER_PATTERN, run_id) is None:
        location = f" in {source}" if source is not None else ""
        raise ProjectStateError(
            f"Invalid VibeSys run ID{location}: {run_id!r}. "
            "Use lowercase letters, digits, dots, underscores, or hyphens."
        )
    return run_id


def _validate_namespace(namespace: str) -> str:
    if re.fullmatch(_IDENTIFIER_PATTERN, namespace) is None:
        raise ProjectStateError(
            f"Invalid VibeSys state namespace: {namespace!r}. "
            "Use lowercase letters, digits, dots, underscores, or hyphens."
        )
    return namespace


def _validate_state_relative_path(raw_path: str | PurePosixPath) -> PurePosixPath:
    if isinstance(raw_path, str):
        value = raw_path
        if not value or "\\" in value or any(not part for part in value.split("/")):
            raise ProjectStateError(
                f"VibeSys state file path must be a non-empty portable relative path: {raw_path!r}"
            )
    else:
        value = raw_path.as_posix()
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path == PurePosixPath(".")
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ProjectStateError(
            f"VibeSys state file path must be a safe portable relative path: {raw_path!r}"
        )
    return path


def _validate_project_state_path(path: PurePosixPath) -> None:
    if not isinstance(path, PurePosixPath):
        raise TypeError("state document paths must be PurePosixPath values")
    try:
        _validate_state_relative_path(path)
    except ProjectStateError as exc:
        raise ValueError(str(exc)) from exc
    if path.parts[:2] != _STATE_DIRECTORY_PARTS or path == _STATE_DIRECTORY_POSIX:
        raise ValueError("state document paths must identify a file below .vibesys/state")


def _validate_snapshot_relative_path(path: PurePosixPath) -> None:
    if not isinstance(path, PurePosixPath):
        raise TypeError("state snapshot paths must be PurePosixPath values")
    try:
        _validate_state_relative_path(path)
    except ProjectStateError as exc:
        raise ValueError(str(exc)) from exc


def _validate_snapshot_root(path: PurePosixPath) -> None:
    _validate_snapshot_relative_path(path)
    parts = path.parts
    if parts == _STATE_DIRECTORY_PARTS:
        return
    if parts[:3] == (*_STATE_DIRECTORY_PARTS, "local"):
        raise ValueError("portable state snapshot root must not be below .vibesys/state/local")
    if parts[:3] != (*_STATE_DIRECTORY_PARTS, "runs") or len(parts) not in {4, 5}:
        raise ValueError(
            "portable state snapshot root must be .vibesys/state, "
            ".vibesys/state/runs/<run-id>, or "
            ".vibesys/state/runs/<run-id>/<namespace>"
        )
    if re.fullmatch(_IDENTIFIER_PATTERN, parts[3]) is None:
        raise ValueError(f"portable state snapshot root contains an invalid run ID: {path}")
    if len(parts) == 5 and re.fullmatch(_IDENTIFIER_PATTERN, parts[4]) is None:  # noqa: PLR2004
        raise ValueError(f"portable state snapshot root contains an invalid namespace: {path}")


def _snapshot_selected_files(
    *,
    project_root: Path,
    root: Path,
    paths: tuple[Path, ...],
) -> StateSnapshot:
    snapshot_root = _contained_without_symlinks(
        project_root,
        root,
        kind="portable snapshot root",
    )
    files: list[StateFile] = []
    try:
        for raw_path in paths:
            path = _contained_without_symlinks(
                snapshot_root,
                raw_path,
                kind="portable snapshot file",
            )
            if not path.is_file():
                if not path.exists():
                    raise ProjectStateError(
                        f"Portable VibeSys snapshot file does not exist: {path}"
                    )
                raise ProjectStateError(f"Portable VibeSys snapshot path is not a file: {path}")
            files.append(
                StateFile(
                    relative_path=PurePosixPath(path.relative_to(snapshot_root).as_posix()),
                    contents=path.read_bytes(),
                )
            )
    except OSError as exc:
        raise ProjectStateError(
            f"Could not read portable VibeSys snapshot below {snapshot_root}: {exc}"
        ) from exc
    files.sort(key=lambda item: item.relative_path.as_posix())
    return StateSnapshot._create(
        PurePosixPath(snapshot_root.relative_to(project_root).as_posix()),
        tuple(files),
    )


def _snapshot_directory(*, project_root: Path, root: Path) -> StateSnapshot:
    snapshot_root = _contained_without_symlinks(
        project_root,
        root,
        kind="portable snapshot root",
    )
    if not snapshot_root.exists():
        return StateSnapshot._create(
            PurePosixPath(snapshot_root.relative_to(project_root).as_posix()),
            (),
        )
    try:
        entries = tuple(
            sorted(
                snapshot_root.rglob("*"),
                key=lambda item: item.relative_to(snapshot_root).as_posix(),
            )
        )
        symlinks = tuple(path for path in entries if path.is_symlink())
        if symlinks:
            raise ProjectStateError(
                f"Portable VibeSys snapshot must not contain symlinks: {symlinks[0]}"
            )
        paths = tuple(path for path in entries if not path.is_dir())
    except OSError as exc:
        raise ProjectStateError(
            f"Could not inspect portable VibeSys snapshot below {snapshot_root}: {exc}"
        ) from exc
    return _snapshot_selected_files(
        project_root=project_root,
        root=snapshot_root,
        paths=paths,
    )


def _contained_state_dir(parent: Path, namespace: str, *, kind: str) -> Path:
    path = _contained_without_symlinks(
        parent,
        parent / _validate_namespace(namespace),
        kind=f"{kind} state",
    )
    try:
        if path.exists() and not path.is_dir():
            raise ProjectStateError(f"VibeSys {kind} state path is not a directory: {path}")
    except OSError as exc:
        raise ProjectStateError(
            f"Could not validate VibeSys {kind} state directory {path}: {exc}"
        ) from exc
    return path


def _contained_without_symlinks(parent: Path, child: Path, *, kind: str) -> Path:
    path = _contained(parent, child)
    current = parent
    for component in child.relative_to(parent).parts:
        current /= component
        try:
            if current.is_symlink():
                raise ProjectStateError(f"VibeSys {kind} path must not be a symlink: {current}")
        except OSError as exc:
            raise ProjectStateError(
                f"Could not validate VibeSys {kind} path {current}: {exc}"
            ) from exc
    return path


def _contained(parent: Path, child: Path) -> Path:
    parent_resolved = parent.resolve()
    child_resolved = child.resolve()
    if not child_resolved.is_relative_to(parent_resolved):
        raise ProjectStateError(f"VibeSys metadata path escapes {parent_resolved}: {child}")
    return child


def _validate_storage_root(path: Path, parent: Path, *, name: str) -> None:
    try:
        if path.is_symlink():
            raise ProjectStateError(f"VibeSys {name} root must not be a symlink: {path}")
        if path.exists() and not path.is_dir():
            raise ProjectStateError(f"VibeSys {name} root is not a directory: {path}")
        parent_resolved = parent.resolve()
        path_resolved = path.resolve()
    except OSError as exc:
        raise ProjectStateError(f"Could not validate VibeSys {name} root {path}: {exc}") from exc
    if not path_resolved.is_relative_to(parent_resolved):
        raise ProjectStateError(
            f"VibeSys {name} root escapes {parent_resolved}: {path} resolves to {path_resolved}"
        )


def _is_excluded(relative: Path) -> bool:
    if relative.parts == (_CONFIG_DIRECTORY_NAME,) or relative.parts[:2] == _STATE_DIRECTORY_PARTS:
        return True
    for part in relative.parts:
        if part in _EXCLUDED_NAMES or part == ".env" or part.startswith(".env."):
            return True
        if part.endswith((".pyc", ".pyo")):
            return True
    return False


def _update_fingerprint(digest: _Digest, path: Path, relative: Path) -> None:
    update = digest.update
    encoded_path = relative.as_posix().encode("utf-8", "surrogateescape")
    try:
        metadata = path.lstat()
        mode = metadata.st_mode
        if stat.S_ISDIR(mode):
            update(b"D\0" + encoded_path + b"\0")
        elif stat.S_ISLNK(mode):
            target = str(path.readlink()).encode("utf-8", "surrogateescape")
            update(b"L\0" + encoded_path + b"\0" + target + b"\0")
        elif stat.S_ISREG(mode):
            executable = b"1" if mode & 0o111 else b"0"
            update(b"F\0" + encoded_path + b"\0" + executable + b"\0")
            with path.open("rb") as source:
                while block := source.read(1024 * 1024):
                    update(block)
            update(b"\0")
        else:
            raise ProjectStateError(f"Unsupported input file type: {path}")
    except OSError as exc:
        raise ProjectStateError(f"Could not fingerprint project input {path}: {exc}") from exc


def _require_current_run_schema(path: Path, run_id: str) -> None:
    """Reject a run manifest written before the current run schema version.

    Only an outdated version is diagnosed here; every other defect is left to
    the model validation that follows, which reports the offending field.
    """
    if not path.exists():
        return
    recorded_version = _read_json_object(path).get("schema_version")
    if not isinstance(recorded_version, int) or recorded_version >= RUN_SCHEMA_VERSION:
        return
    missing_contract = (
        "the runtime environment the run executes in"
        if recorded_version == 1
        else "portable compute-resource requirements"
    )
    raise RunSchemaMigrationRequiredError(
        f"Run metadata at {path} uses run schema version {recorded_version}, but this "
        f"VibeSys requires version {RUN_SCHEMA_VERSION}, which records "
        f"{missing_contract}. Migrate the run with the environment it was launched "
        "with; VibeSys cannot infer missing execution metadata.",
        path=path,
        run_id=run_id,
        recorded_version=recorded_version,
    )
