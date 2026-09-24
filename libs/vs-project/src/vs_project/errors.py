"""Shared errors for the VibeSys project boundary."""

from pathlib import Path, PurePosixPath
from typing import Self


class ProjectError(RuntimeError):
    """Base class for invalid project layout or state operations."""


class ProjectStateError(ProjectError):
    """Raised when project metadata is missing, unsafe, or invalid."""

    @classmethod
    def run_id_timestamp_timezone_missing(cls) -> Self:
        """Describe a run ID timestamp that lacks its required timezone."""
        return cls("Run ID timestamp must include a timezone")

    @classmethod
    def metadata_timestamp_timezone_missing(cls) -> Self:
        """Describe a metadata timestamp that lacks its required timezone."""
        return cls("Metadata timestamp must include a timezone")

    @classmethod
    def invalid_run_id(cls, value: str, source: Path | None = None) -> Self:
        """Describe an invalid run ID."""
        location = f" in {source}" if source is not None else ""
        return cls(
            f"Invalid VibeSys run ID{location}: {value!r}. "
            "Use lowercase letters, digits, dots, underscores, or hyphens."
        )

    @classmethod
    def invalid_state_namespace(cls, value: str) -> Self:
        """Describe an invalid state namespace."""
        return cls(
            f"Invalid VibeSys state namespace: {value!r}. "
            "Use lowercase letters, digits, dots, underscores, or hyphens."
        )

    @classmethod
    def invalid_state_file_path(cls, path: str | PurePosixPath, *, portable: bool) -> Self:
        """Describe a state path that is not a safe portable relative path."""
        if portable:
            return cls(
                f"VibeSys state file path must be a non-empty portable relative path: {path!r}"
            )
        return cls(f"VibeSys state file path must be a safe portable relative path: {path!r}")

    @classmethod
    def state_home_empty(cls, variable: str) -> Self:
        """Describe an explicitly configured but empty state-home variable."""
        return cls(f"{variable} must not be empty")

    @classmethod
    def state_home_not_absolute(cls, variable: str, configured: str) -> Self:
        """Describe a relative state-home override."""
        return cls(f"{variable} must be an absolute path: {configured}")

    @classmethod
    def state_home_inside_project(cls, variable: str, path: Path) -> Self:
        """Describe a state-home location inside the project it stores."""
        return cls(f"{variable} must place machine-local state outside the project: {path}")

    @classmethod
    def unsafe_relative_path(cls, path: Path | str) -> Self:
        """Describe a state path that is not safe and relative to its project."""
        return cls(f"Project path must be a safe relative path: {path}")

    @classmethod
    def state_path_not_file(cls, path: Path) -> Self:
        """Describe a state path occupied by a non-file entry."""
        return cls(f"VibeSys state path is not a file: {path}")

    @classmethod
    def state_path_not_directory(cls, kind: str, path: Path) -> Self:
        """Describe a state path occupied by a non-directory entry."""
        return cls(f"VibeSys {kind} state path is not a directory: {path}")

    @classmethod
    def state_path_symlink(cls, kind: str, path: Path) -> Self:
        """Describe a state path that resolves through a symbolic link."""
        return cls(f"VibeSys {kind} path must not be a symlink: {path}")

    @classmethod
    def namespace_snapshot_symlink(cls, kind: str, path: Path) -> Self:
        """Describe a symbolic link inside a state namespace snapshot."""
        return cls(f"VibeSys {kind} state must not contain symlinks: {path}")

    @classmethod
    def namespace_snapshot_unsupported_file(cls, kind: str, path: Path) -> Self:
        """Describe an unsupported entry inside a state namespace snapshot."""
        return cls(f"VibeSys {kind} state contains an unsupported file type: {path}")

    @classmethod
    def namespace_snapshot_failed(cls, kind: str, path: Path, error: Exception) -> Self:
        """Describe a failed read of one state namespace snapshot."""
        return cls(f"Could not snapshot VibeSys {kind} state at {path}: {error}")

    @classmethod
    def transition_outside_namespace(cls, path: Path | PurePosixPath) -> Self:
        """Describe a state transition aimed outside its owning namespace."""
        return cls(f"State transition target is outside this namespace: {path}")

    @classmethod
    def transition_apply_failed(cls, path: Path, error: Exception) -> Self:
        """Describe a failed state transition write or deletion."""
        return cls(f"Could not apply VibeSys state transition at {path}: {error}")

    @classmethod
    def external_state_path_not_directory(cls, kind: str, path: Path) -> Self:
        """Describe an external state path occupied by a non-directory entry."""
        return cls(f"VibeSys {kind} external state path is not a directory: {path}")

    @classmethod
    def external_state_directory_create_failed(
        cls,
        kind: str,
        path: Path,
        error: Exception,
    ) -> Self:
        """Describe a failure creating an external state directory."""
        return cls(f"Could not create VibeSys {kind} external state directory {path}: {error}")

    @classmethod
    def state_namespace_not_directory(cls, kind: str, path: Path) -> Self:
        """Describe a state namespace occupied by a non-directory entry."""
        return cls(f"VibeSys {kind} state namespace is not a directory: {path}")

    @classmethod
    def state_namespace_validation_failed(cls, kind: str, path: Path, error: Exception) -> Self:
        """Describe a failure validating a state namespace directory."""
        return cls(f"Could not validate VibeSys {kind} state namespace {path}: {error}")

    @classmethod
    def local_state_symlink(cls, path: Path) -> Self:
        """Describe a symbolic link inside legacy machine-local state."""
        return cls(f"VibeSys local metadata path must not be a symlink: {path}")

    @classmethod
    def storage_root_symlink(cls, name: str, path: Path) -> Self:
        """Describe a storage root that is a symbolic link."""
        return cls(f"VibeSys {name} root must not be a symlink: {path}")

    @classmethod
    def storage_root_not_directory(cls, name: str, path: Path) -> Self:
        """Describe a storage root occupied by a non-directory entry."""
        return cls(f"VibeSys {name} root is not a directory: {path}")

    @classmethod
    def path_escapes_root(cls, parent: Path, child: Path) -> Self:
        """Describe a metadata path that resolves outside its owning root."""
        return cls(f"VibeSys metadata path escapes {parent}: {child}")

    @classmethod
    def storage_root_escapes(cls, name: str, parent: Path, path: Path, resolved: Path) -> Self:
        """Describe a storage root that resolves outside its allowed parent."""
        return cls(f"VibeSys {name} root escapes {parent}: {path} resolves to {resolved}")

    @classmethod
    def local_state_cannot_snapshot(cls) -> Self:
        """Describe a snapshot request for machine-local state."""
        return cls("Machine-local VibeSys state namespaces cannot be snapshotted")

    @classmethod
    def local_state_not_agent_visible(cls) -> Self:
        """Describe an attempt to expose machine-local state to an agent."""
        return cls("Machine-local VibeSys state is not agent-visible")

    @classmethod
    def local_state_has_no_worktree_equivalent(cls) -> Self:
        """Describe a worktree lookup for machine-local state."""
        return cls("Machine-local VibeSys state has no worktree equivalent")

    @classmethod
    def snapshot_from_deletion_transition(cls) -> Self:
        """Describe an attempt to snapshot a deletion transition."""
        return cls("Cannot create a state snapshot from a deletion transition")

    @classmethod
    def serialized_transition_invalid_json(cls) -> Self:
        """Describe transition bytes that are not valid JSON."""
        return cls("Serialized state transition is not valid JSON")

    @classmethod
    def serialized_transition_not_object(cls) -> Self:
        """Describe transition JSON whose root is not an object."""
        return cls("Serialized state transition must be a JSON object")

    @classmethod
    def serialized_transition_invalid_schema(cls) -> Self:
        """Describe a transition with an unsupported serialized schema."""
        return cls("Serialized state transition has an invalid schema")

    @classmethod
    def serialized_transition_document_not_object(cls) -> Self:
        """Describe a transition document whose value is not an object."""
        return cls("Serialized state transition document must be an object")

    @classmethod
    def serialized_transition_model_mismatch(cls, error: Exception) -> Self:
        """Describe a serialized replacement that does not match its slot model."""
        return cls(f"Serialized state transition does not match the slot schema: {error}")

    @classmethod
    def typed_transition_target_mismatch(
        cls,
        expected: Path | PurePosixPath | str,
        actual: Path | PurePosixPath | str,
    ) -> Self:
        """Describe a transition targeting a different typed state slot."""
        return cls(
            f"State transition must target the typed slot: expected {expected}, got {actual}"
        )

    @classmethod
    def typed_transition_model_mismatch(
        cls,
        path: Path | PurePosixPath | str,
        error: Exception,
    ) -> Self:
        """Describe a replacement that does not match its typed state slot."""
        return cls(
            f"State transition document does not match the typed slot schema at {path}: {error}"
        )

    @classmethod
    def portable_snapshot_file_missing(cls, path: Path) -> Self:
        """Describe a required portable snapshot file that is missing."""
        return cls(f"Portable VibeSys snapshot file does not exist: {path}")

    @classmethod
    def portable_snapshot_path_not_file(cls, path: Path) -> Self:
        """Describe a portable snapshot path occupied by a non-file entry."""
        return cls(f"Portable VibeSys snapshot path is not a file: {path}")

    @classmethod
    def portable_snapshot_contains_symlink(cls, path: Path) -> Self:
        """Describe a symbolic link in a portable snapshot tree."""
        return cls(f"Portable VibeSys snapshot must not contain symlinks: {path}")

    @classmethod
    def portable_snapshot_read_failed(cls, root: Path, error: Exception) -> Self:
        """Describe a failure reading the selected portable snapshot files."""
        return cls(f"Could not read portable VibeSys snapshot below {root}: {error}")

    @classmethod
    def portable_snapshot_inspection_failed(cls, root: Path, error: Exception) -> Self:
        """Describe a failure inspecting a portable snapshot tree."""
        return cls(f"Could not inspect portable VibeSys snapshot below {root}: {error}")

    @classmethod
    def completed_round_data_conflict(cls, path: Path) -> Self:
        """Describe an attempt to replace an existing round with different data."""
        return cls(f"Completed round already exists with different data: {path}")

    @classmethod
    def completed_round_append_out_of_order(cls, expected: int, actual: int) -> Self:
        """Describe an appended round that skips or rewrites sequence order."""
        return cls(
            f"Completed rounds must be appended in order: expected round {expected}, got {actual}"
        )

    @classmethod
    def completed_rounds_not_contiguous(cls, expected: int, actual: int, path: Path) -> Self:
        """Describe a gap in the completed-round sequence."""
        return cls(
            "Completed rounds must form a contiguous sequence starting at 1: "
            f"expected round {expected}, found {actual} at {path}"
        )

    @classmethod
    def completed_round_file_mismatch(cls, path: Path, actual: int, expected: int) -> Self:
        """Describe a round file whose contents name a different round."""
        return cls(f"Round file {path} contains round {actual}, expected {expected}")

    @classmethod
    def completed_round_missing(cls, round_number: int, run_id: str) -> Self:
        """Describe a requested completed round that is absent from its run."""
        return cls(f"Completed round {round_number} does not exist for run {run_id!r}")

    @classmethod
    def restore_before_existing_round(cls, round_number: int, existing_round: int) -> Self:
        """Describe a restore that would leave a later completed round in place."""
        return cls(f"Cannot restore round {round_number} before existing round {existing_round}")

    @classmethod
    def restore_without_predecessor(cls, round_number: int, predecessor: int) -> Self:
        """Describe a restore whose preceding completed round is missing."""
        return cls(f"Cannot restore round {round_number} without completed round {predecessor}")

    @classmethod
    def duplicate_completed_round(cls, number: int, first: Path, duplicate: Path) -> Self:
        """Describe two completed-round files that claim the same sequence number."""
        return cls(f"Duplicate completed-round number {number}: {first} and {duplicate}")

    @classmethod
    def invalid_portable_round(cls, subject: str, field_name: str) -> Self:
        """Describe a completed-round path that cannot be persisted portably."""
        return cls(f"{subject} {field_name} must be a portable project-relative path")

    @classmethod
    def non_finite_round_metrics(cls, subject: str) -> Self:
        """Describe non-finite metrics in completed-round metadata."""
        return cls(f"{subject} metrics must be finite numbers")

    @classmethod
    def invalid_round_number(cls, round_number: int) -> Self:
        """Describe a completed round with a non-positive number."""
        return cls(f"Round number must be positive, got {round_number}")

    @classmethod
    def project_root_not_directory(cls, path: Path) -> Self:
        """Describe a project root path that is not a directory."""
        return cls(f"Project root is not a directory: {path}")

    @classmethod
    def unexpected_completed_round_entry(cls, path: Path) -> Self:
        """Describe a non-round file in the completed-round directory."""
        return cls(f"Unexpected completed-round entry: {path}")

    @classmethod
    def round_serialization_failed(cls, round_number: int) -> Self:
        """Describe a completed round that could not be serialized."""
        return cls(f"Could not serialize completed-round metadata for round {round_number}")

    @classmethod
    def state_serialization_failed(cls) -> Self:
        """Describe a state model that could not be serialized."""
        return cls("Could not serialize VibeSys state model")

    @classmethod
    def json_serialization_failed(cls, subject: str) -> Self:
        """Describe a state object that could not be serialized."""
        return cls(f"Could not serialize {subject}")

    @classmethod
    def metadata_file_missing(cls, path: Path) -> Self:
        """Describe a missing metadata file."""
        return cls(f"VibeSys metadata file does not exist: {path}")

    @classmethod
    def metadata_read_failed(cls, path: Path, error: Exception) -> Self:
        """Describe a metadata read failure and its path."""
        return cls(f"Could not read VibeSys metadata at {path}: {error}")

    @classmethod
    def metadata_not_object(cls, path: Path) -> Self:
        """Describe metadata whose JSON root is not an object."""
        return cls(f"Expected a JSON object in VibeSys metadata at {path}")

    @classmethod
    def invalid_metadata(cls, path: Path, details: str) -> Self:
        """Describe a metadata validation failure with stable field details."""
        return cls(f"Invalid VibeSys metadata at {path}: {details}")

    @classmethod
    def state_read_failed(cls, path: Path, error: Exception) -> Self:
        """Describe an operational-state read failure and its path."""
        return cls(f"Could not read VibeSys state model at {path}: {error}")

    @classmethod
    def invalid_state_model(cls, path: Path, details: str) -> Self:
        """Describe an operational-state validation failure."""
        return cls(f"Invalid VibeSys state model at {path}: {details}")


class StateModelNotFoundError(ProjectStateError):
    """Raised when a required model is absent from a state namespace."""

    @classmethod
    def missing(cls, path: Path) -> Self:
        """Describe a missing required operational-state model."""
        return cls(f"VibeSys state model does not exist: {path}")


class RunSchemaMigrationRequiredError(ProjectStateError):
    """Raised when a run manifest predates the current run schema version.

    Callers own the operator-facing migration instructions; this package
    reports only the offending path and the two schema versions.
    """

    @classmethod
    def older_schema(
        cls,
        *,
        path: Path,
        run_id: str,
        recorded_version: int,
        required_version: int,
        missing_contract: str,
    ) -> Self:
        """Describe the metadata missing from a run recorded by an older schema."""
        message = (
            f"Run metadata at {path} uses run schema version {recorded_version}, but this "
            f"VibeSys requires version {required_version}, which records "
            f"{missing_contract}. Migrate the run with the environment it was launched "
            "with; VibeSys cannot infer missing execution metadata."
        )
        return cls(
            message,
            path=path,
            run_id=run_id,
            recorded_version=recorded_version,
        )

    def __init__(self, message: str, *, path: Path, run_id: str, recorded_version: int) -> None:
        """Initialize the migration error and its structured context."""
        super().__init__(message)
        self.path = path
        self.run_id = run_id
        self.recorded_version = recorded_version
