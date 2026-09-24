"""Message contracts for the named ProjectStateError constructors."""

from pathlib import Path, PurePosixPath

import pytest

from vs_project.errors import (
    ProjectError,
    ProjectStateError,
    RunSchemaMigrationRequiredError,
    StateModelNotFoundError,
)

_P = Path("/proj/x")
_ERR = ValueError("boom")

_CASES: list[tuple[str, tuple[object, ...], str]] = [
    ("run_id_timestamp_timezone_missing", (), "Run ID timestamp must include a timezone"),
    ("metadata_timestamp_timezone_missing", (), "Metadata timestamp must include a timezone"),
    ("invalid_run_id", ("Bad", None), "Invalid VibeSys run ID: 'Bad'. Use lowercase"),
    ("invalid_run_id", ("Bad", _P), "Invalid VibeSys run ID in /proj/x: 'Bad'."),
    ("invalid_state_namespace", ("A",), "Invalid VibeSys state namespace: 'A'."),
    ("state_home_empty", ("VAR",), "VAR must not be empty"),
    ("state_home_not_absolute", ("VAR", "rel"), "VAR must be an absolute path: rel"),
    ("state_home_inside_project", ("VAR", _P), "VAR must place machine-local state outside"),
    ("unsafe_relative_path", ("../x",), "Project path must be a safe relative path: ../x"),
    ("state_path_not_file", (_P,), "VibeSys state path is not a file: /proj/x"),
    (
        "state_path_not_directory",
        ("run", _P),
        "VibeSys run state path is not a directory: /proj/x",
    ),
    ("state_path_symlink", ("run", _P), "VibeSys run path must not be a symlink: /proj/x"),
    (
        "namespace_snapshot_symlink",
        ("run", _P),
        "VibeSys run state must not contain symlinks: /proj/x",
    ),
    (
        "namespace_snapshot_unsupported_file",
        ("run", _P),
        "VibeSys run state contains an unsupported file type: /proj/x",
    ),
    (
        "namespace_snapshot_failed",
        ("run", _P, _ERR),
        "Could not snapshot VibeSys run state at /proj/x: boom",
    ),
    (
        "transition_outside_namespace",
        (PurePosixPath("a/b"),),
        "State transition target is outside this namespace: a/b",
    ),
    (
        "transition_apply_failed",
        (_P, _ERR),
        "Could not apply VibeSys state transition at /proj/x: boom",
    ),
    (
        "external_state_path_not_directory",
        ("run", _P),
        "VibeSys run external state path is not a directory: /proj/x",
    ),
    (
        "external_state_directory_create_failed",
        ("run", _P, _ERR),
        "Could not create VibeSys run external state directory /proj/x: boom",
    ),
    (
        "state_namespace_not_directory",
        ("run", _P),
        "VibeSys run state namespace is not a directory: /proj/x",
    ),
    (
        "state_namespace_validation_failed",
        ("run", _P, _ERR),
        "Could not validate VibeSys run state namespace /proj/x: boom",
    ),
    (
        "local_state_symlink",
        (_P,),
        "VibeSys local metadata path must not be a symlink: /proj/x",
    ),
    ("storage_root_symlink", ("runs", _P), "VibeSys runs root must not be a symlink: /proj/x"),
    (
        "storage_root_not_directory",
        ("runs", _P),
        "VibeSys runs root is not a directory: /proj/x",
    ),
    ("path_escapes_root", (Path("/p"), _P), "VibeSys metadata path escapes /p: /proj/x"),
    (
        "storage_root_escapes",
        ("runs", Path("/p"), _P, Path("/q")),
        "VibeSys runs root escapes /p: /proj/x resolves to /q",
    ),
    (
        "local_state_cannot_snapshot",
        (),
        "Machine-local VibeSys state namespaces cannot be snapshotted",
    ),
    (
        "local_state_not_agent_visible",
        (),
        "Machine-local VibeSys state is not agent-visible",
    ),
    (
        "local_state_has_no_worktree_equivalent",
        (),
        "Machine-local VibeSys state has no worktree equivalent",
    ),
    (
        "snapshot_from_deletion_transition",
        (),
        "Cannot create a state snapshot from a deletion transition",
    ),
    (
        "serialized_transition_invalid_json",
        (),
        "Serialized state transition is not valid JSON",
    ),
    (
        "serialized_transition_not_object",
        (),
        "Serialized state transition must be a JSON object",
    ),
    (
        "serialized_transition_invalid_schema",
        (),
        "Serialized state transition has an invalid schema",
    ),
    (
        "serialized_transition_document_not_object",
        (),
        "Serialized state transition document must be an object",
    ),
    (
        "serialized_transition_model_mismatch",
        (_ERR,),
        "Serialized state transition does not match the slot schema: boom",
    ),
    (
        "typed_transition_target_mismatch",
        ("a", "b"),
        "State transition must target the typed slot: expected a, got b",
    ),
    (
        "typed_transition_model_mismatch",
        ("a", _ERR),
        "State transition document does not match the typed slot schema at a: boom",
    ),
    (
        "portable_snapshot_file_missing",
        (_P,),
        "Portable VibeSys snapshot file does not exist: /proj/x",
    ),
    (
        "portable_snapshot_path_not_file",
        (_P,),
        "Portable VibeSys snapshot path is not a file: /proj/x",
    ),
    (
        "portable_snapshot_contains_symlink",
        (_P,),
        "Portable VibeSys snapshot must not contain symlinks: /proj/x",
    ),
    (
        "portable_snapshot_read_failed",
        (_P, _ERR),
        "Could not read portable VibeSys snapshot below /proj/x: boom",
    ),
    (
        "portable_snapshot_inspection_failed",
        (_P, _ERR),
        "Could not inspect portable VibeSys snapshot below /proj/x: boom",
    ),
    (
        "completed_round_data_conflict",
        (_P,),
        "Completed round already exists with different data: /proj/x",
    ),
    (
        "completed_round_append_out_of_order",
        (2, 4),
        "Completed rounds must be appended in order: expected round 2, got 4",
    ),
    (
        "completed_rounds_not_contiguous",
        (2, 4, _P),
        "expected round 2, found 4 at /proj/x",
    ),
    (
        "completed_round_file_mismatch",
        (_P, 3, 2),
        "Round file /proj/x contains round 3, expected 2",
    ),
    (
        "completed_round_missing",
        (3, "r1"),
        "Completed round 3 does not exist for run 'r1'",
    ),
    (
        "restore_before_existing_round",
        (1, 2),
        "Cannot restore round 1 before existing round 2",
    ),
    (
        "restore_without_predecessor",
        (3, 2),
        "Cannot restore round 3 without completed round 2",
    ),
    (
        "duplicate_completed_round",
        (2, _P, Path("/proj/y")),
        "Duplicate completed-round number 2: /proj/x and /proj/y",
    ),
    (
        "invalid_portable_round",
        ("Round", "workspace"),
        "Round workspace must be a portable project-relative path",
    ),
    ("non_finite_round_metrics", ("Round",), "Round metrics must be finite numbers"),
    ("invalid_round_number", (0,), "Round number must be positive, got 0"),
    ("project_root_not_directory", (_P,), "Project root is not a directory: /proj/x"),
    (
        "unexpected_completed_round_entry",
        (_P,),
        "Unexpected completed-round entry: /proj/x",
    ),
    (
        "round_serialization_failed",
        (5,),
        "Could not serialize completed-round metadata for round 5",
    ),
    ("state_serialization_failed", (), "Could not serialize VibeSys state model"),
    ("json_serialization_failed", ("thing",), "Could not serialize thing"),
    ("metadata_file_missing", (_P,), "VibeSys metadata file does not exist: /proj/x"),
    (
        "metadata_read_failed",
        (_P, _ERR),
        "Could not read VibeSys metadata at /proj/x: boom",
    ),
    (
        "metadata_not_object",
        (_P,),
        "Expected a JSON object in VibeSys metadata at /proj/x",
    ),
    (
        "invalid_metadata",
        (_P, "field x"),
        "Invalid VibeSys metadata at /proj/x: field x",
    ),
    (
        "state_read_failed",
        (_P, _ERR),
        "Could not read VibeSys state model at /proj/x: boom",
    ),
    (
        "invalid_state_model",
        (_P, "field x"),
        "Invalid VibeSys state model at /proj/x: field x",
    ),
]


@pytest.mark.parametrize(("factory", "args", "expected"), _CASES)
def test_project_state_error_factory_message(
    factory: str, args: tuple[object, ...], expected: str
) -> None:
    error = getattr(ProjectStateError, factory)(*args)

    assert isinstance(error, ProjectStateError)
    assert isinstance(error, ProjectError)
    assert expected in str(error)


def test_invalid_state_file_path_distinguishes_portable_requirement() -> None:
    portable = ProjectStateError.invalid_state_file_path("", portable=True)
    unsafe = ProjectStateError.invalid_state_file_path("../a", portable=False)

    assert "non-empty portable relative path: ''" in str(portable)
    assert "safe portable relative path: '../a'" in str(unsafe)


def test_state_model_not_found_names_missing_path() -> None:
    error = StateModelNotFoundError.missing(_P)

    assert isinstance(error, ProjectStateError)
    assert str(error) == "VibeSys state model does not exist: /proj/x"


def test_run_schema_migration_error_carries_structured_context() -> None:
    error = RunSchemaMigrationRequiredError.older_schema(
        path=_P,
        run_id="r1",
        recorded_version=1,
        required_version=3,
        missing_contract="the device lease",
    )

    assert (error.path, error.run_id, error.recorded_version) == (_P, "r1", 1)
    message = str(error)
    assert "run schema version 1" in message
    assert "requires version 3, which records the device lease" in message
