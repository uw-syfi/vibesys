"""Local recipe trust and reuse rules owned by profile multi."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vibesys.loops.profile_multi.validation import (
    _load_validation_recipes,
    _reusable_validation_result,
    _validation_input_digest,
)
from vibesys.schemas import FrameworkValidationResult, ValidationRecipe, ValidationRecipeArtifact

if TYPE_CHECKING:
    from pathlib import Path


def _recipe(*, input_paths: list[str] | None = None) -> ValidationRecipe:
    return ValidationRecipe(
        name="focused",
        command="python -m check",
        input_paths=input_paths or ["candidate.py"],
        timeout_seconds=30,
        purpose="Check the edited candidate",
    )


def test_digest_tracks_inputs_command_and_timeout(tmp_path: Path) -> None:
    source = tmp_path / "candidate.py"
    source.write_text("a = 1\n")
    recipe = _recipe()
    first = _validation_input_digest(tmp_path, recipe)
    assert first == _validation_input_digest(tmp_path, recipe)

    source.write_text("a = 2\n")
    assert _validation_input_digest(tmp_path, recipe) != first
    assert _validation_input_digest(
        tmp_path, recipe.model_copy(update={"command": "python -m another_check"})
    ) != _validation_input_digest(tmp_path, recipe)
    assert _validation_input_digest(
        tmp_path, recipe.model_copy(update={"timeout_seconds": 31})
    ) != _validation_input_digest(tmp_path, recipe)


def test_digest_rejects_missing_and_symlink_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _validation_input_digest(tmp_path, _recipe())

    outside = tmp_path.parent / "outside.py"
    outside.write_text("outside\n")
    (tmp_path / "candidate.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        _validation_input_digest(tmp_path, _recipe())


def test_digest_hashes_directory_contents(tmp_path: Path) -> None:
    directory = tmp_path / "src"
    directory.mkdir()
    (directory / "a.py").write_text("a\n")
    recipe = _recipe(input_paths=["src"])
    first = _validation_input_digest(tmp_path, recipe)
    (directory / "b.py").write_text("b\n")
    assert _validation_input_digest(tmp_path, recipe) != first


def test_recipe_artifact_stays_inside_workspace_and_validates_schema(tmp_path: Path) -> None:
    artifact = tmp_path / "recipes.json"
    artifact.write_text(ValidationRecipeArtifact(recipes=[_recipe()]).model_dump_json())
    assert _load_validation_recipes(tmp_path, "recipes.json") == [_recipe()]

    with pytest.raises(ValueError, match="escapes"):
        _load_validation_recipes(tmp_path, "../outside.json")
    with pytest.raises(ValueError, match="does not exist"):
        _load_validation_recipes(tmp_path, "missing.json")
    artifact.write_text("not JSON")
    with pytest.raises(ValueError, match="not valid JSON"):
        _load_validation_recipes(tmp_path, "recipes.json")
    artifact.write_text(json.dumps({"version": 2, "recipes": []}))
    with pytest.raises(ValueError, match="does not match version 1"):
        _load_validation_recipes(tmp_path, "recipes.json")


def test_reuse_requires_matching_recipe_digest_and_prior_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _recipe()
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_text("broken JSON")
    passing = FrameworkValidationResult(recipe=recipe, input_digest="abc", passed=True)
    failing = FrameworkValidationResult(recipe=recipe, input_digest="abc", passed=False)
    new.write_text(json.dumps({"results": [passing.model_dump(), failing.model_dump()]}))
    monkeypatch.setattr(
        "vibesys.loops.profile_multi.validation.issue_board.validation_result_artifact_paths",
        lambda _path: [old, new],
    )

    reused = _reusable_validation_result(tmp_path / "progress.md", recipe, "abc")
    assert reused is not None
    assert reused.passed
    assert reused.reused
    assert _reusable_validation_result(tmp_path / "progress.md", recipe, "changed") is None
    assert (
        _reusable_validation_result(
            tmp_path / "progress.md", recipe.model_copy(update={"command": "different"}), "abc"
        )
        is None
    )
