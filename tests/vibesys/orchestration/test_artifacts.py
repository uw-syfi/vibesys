"""``vibesys.orchestration.artifacts``: host-owned turn-artifact writers.

Every writer here is a pure function of a ``progress_path`` and a payload: no
``RunContext`` is needed to exercise the public API. These tests cover the
atomic-write primitive (``write_json``/``write_model``), path derivation for
each artifact kind under both memory layouts (legacy ``progress.md`` and the
directory layout), and the durable-attempt-numbering contract
(``next_implementer_attempt``) that survives a process killed mid-invoke.

``FrameworkValidationResult``/``ValidationRecipeArtifact`` are imported from
this module rather than their owning module directly: their owning module
moves across the stack (``vibesys.schemas`` here, later
``vibesys.evaluators.validation_recipe``), but ``artifacts`` always binds
them under these names.
"""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING

import pytest
from pydantic import BaseModel

from vibesys.orchestration.artifacts import (
    FrameworkValidationResult,
    ImplementerStartMarker,
    ValidationRecipeArtifact,
    implementer_artifact_path,
    implementer_artifact_paths,
    next_implementer_attempt,
    plan_artifact_path,
    profiler_artifact_root,
    validation_artifact_root,
    validation_recipe_schema_path,
    validation_result_artifact_paths,
    write_implementer_start_marker,
    write_json,
    write_model,
    write_validation_recipe_schema,
    write_validation_result_artifact,
)
from vibesys.orchestration.memory import structured_artifact_root


def _validation_recipe_cls() -> type:
    """Resolve ``ValidationRecipe`` by dynamic lookup.

    Its owning module moves across the stack (``vibesys.schemas`` here,
    later ``vibesys.evaluators.validation_recipe``); a static import of the
    not-yet-existing later location would fail type checking on this branch,
    so this runs the lookup by name instead of statically importing either
    candidate.
    """
    for module_name in ("vibesys.evaluators.validation_recipe", "vibesys.schemas"):
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        candidate = getattr(module, "ValidationRecipe", None)
        if candidate is not None:
            return candidate
    raise ImportError("ValidationRecipe not found in any known location")  # noqa: TRY003  # tracked: #288


ValidationRecipe = _validation_recipe_cls()

if TYPE_CHECKING:
    from pathlib import Path


def _recipe(name: str = "focused-tests"):  # noqa: ANN202  # tracked: #288 -- return type resolved dynamically above
    return ValidationRecipe(
        name=name,
        command="uv run pytest -q test_server.py",
        input_paths=["server.py"],
        purpose="Exercise the focused local server contract.",
    )


class _Payload(BaseModel):
    label: str
    count: int


# ---------------------------------------------------------------------------
# write_json / write_model: the shared atomic-write primitive
# ---------------------------------------------------------------------------


def test_write_json_creates_parents_and_round_trips(tmp_path: Path) -> None:
    destination = tmp_path / "nested" / "dir" / "payload.json"

    result = write_json(destination, {"a": 1, "b": [1, 2, 3]})

    assert result == destination
    assert json.loads(destination.read_text()) == {"a": 1, "b": [1, 2, 3]}


def test_write_json_overwrites_existing_file_atomically(tmp_path: Path) -> None:
    destination = tmp_path / "payload.json"
    write_json(destination, {"version": 1})

    write_json(destination, {"version": 2})

    assert json.loads(destination.read_text()) == {"version": 2}
    # No leftover temp file from the atomic replace.
    assert list(tmp_path.iterdir()) == [destination]


def test_write_model_dumps_json_mode(tmp_path: Path) -> None:
    destination = tmp_path / "model.json"

    write_model(destination, _Payload(label="x", count=3))

    assert json.loads(destination.read_text()) == {"label": "x", "count": 3}


# ---------------------------------------------------------------------------
# Path derivation, for both memory layouts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "progress_path_name",
    ["progress.md", "progress"],
)
def test_plan_artifact_path_is_under_structured_root(
    tmp_path: Path, progress_path_name: str
) -> None:
    progress_path = tmp_path / progress_path_name

    path = plan_artifact_path(progress_path, round_number=12)

    assert path == structured_artifact_root(progress_path) / "plans" / "round-0012.json"


def test_implementer_artifact_path_is_under_evidence_dir(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"

    path = implementer_artifact_path(progress_path, round_number=3, retry=2)

    assert path == (
        structured_artifact_root(progress_path)
        / "evidence"
        / "round-0003-attempt-02-implementer.json"
    )


def test_validation_artifact_root_and_recipe_schema_path(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"

    root = validation_artifact_root(progress_path)
    schema_path = validation_recipe_schema_path(progress_path)

    assert root == structured_artifact_root(progress_path) / "validation"
    assert schema_path == root / "recipe-schema.json"


def test_profiler_artifact_root(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"

    root = profiler_artifact_root(progress_path, round_number=7)

    assert root == structured_artifact_root(progress_path) / "profiles" / "round-0007"


def test_write_validation_recipe_schema_publishes_json_schema(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"

    path = write_validation_recipe_schema(progress_path)

    assert path == validation_recipe_schema_path(progress_path)
    schema = json.loads(path.read_text())
    assert schema == ValidationRecipeArtifact.model_json_schema(mode="validation")


# ---------------------------------------------------------------------------
# Validation results
# ---------------------------------------------------------------------------


def test_write_and_list_validation_result_artifacts_in_creation_order(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"
    results = [
        FrameworkValidationResult(
            recipe=_recipe(), input_digest="deadbeef", passed=True, exit_code=0
        )
    ]

    write_validation_result_artifact(progress_path, round_number=1, retry=1, results=results)
    write_validation_result_artifact(progress_path, round_number=2, retry=1, results=results)

    paths = validation_result_artifact_paths(progress_path)
    assert [p.name for p in paths] == [
        "round-0001-attempt-01.json",
        "round-0002-attempt-01.json",
    ]
    payload = json.loads(paths[0].read_text())
    assert payload["round"] == 1
    assert payload["attempt"] == 1
    assert payload["results"][0]["passed"] is True
    assert payload["results"][0]["recipe"]["name"] == "focused-tests"


def test_validation_result_artifact_paths_empty_when_none_written(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"

    assert validation_result_artifact_paths(progress_path) == []


# ---------------------------------------------------------------------------
# Implementer attempt numbering: crash-safety contract
# ---------------------------------------------------------------------------


def test_next_implementer_attempt_starts_at_one(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"

    assert next_implementer_attempt(progress_path, round_number=1) == 1


def test_next_implementer_attempt_increments_past_completed_artifacts(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"
    write_model(
        implementer_artifact_path(progress_path, round_number=1, retry=1),
        _Payload(label="attempt-1", count=1),
    )

    assert next_implementer_attempt(progress_path, round_number=1) == 2


def test_next_implementer_attempt_counts_start_markers_too(tmp_path: Path) -> None:
    """A killed process leaves only a start marker; the next attempt still advances.

    This is the durability property: attempt numbering must not replay a
    killed attempt's label, so a marker with no matching completed artifact
    still counts.
    """
    progress_path = tmp_path / "progress.md"

    write_implementer_start_marker(progress_path, round_number=1, retry=1)

    assert next_implementer_attempt(progress_path, round_number=1) == 2


def test_write_implementer_start_marker_records_round_and_attempt(tmp_path: Path) -> None:
    progress_path = tmp_path / "progress.md"

    path = write_implementer_start_marker(progress_path, round_number=4, retry=2)

    payload = json.loads(path.read_text())
    assert payload == ImplementerStartMarker(round=4, attempt=2).model_dump(mode="json")


def test_implementer_artifact_paths_only_matches_completed_suffix(tmp_path: Path) -> None:
    """A start marker for an in-flight attempt is not a completed artifact."""
    progress_path = tmp_path / "progress.md"
    write_implementer_start_marker(progress_path, round_number=1, retry=1)
    completed = write_model(
        implementer_artifact_path(progress_path, round_number=1, retry=2),
        _Payload(label="done", count=1),
    )

    paths = implementer_artifact_paths(progress_path, round_number=1)

    assert paths == [completed]
