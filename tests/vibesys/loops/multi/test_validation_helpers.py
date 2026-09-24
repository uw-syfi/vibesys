"""Candidate-authored local validation stays bounded to its declared inputs."""

# ruff: noqa: SLF001  # Exercise the policy's focused validation helpers directly.

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from vibesys.loops.multi import validation
from vibesys.schemas import FrameworkValidationResult, ValidationRecipe, ValidationRecipeArtifact

if TYPE_CHECKING:
    from pathlib import Path


def _recipe(*, input_paths: list[str] | None = None) -> ValidationRecipe:
    return ValidationRecipe(
        name="focused-tests",
        command="python -m pytest tests/test_server.py",
        input_paths=input_paths or ["src"],
        purpose="Check the changed server behavior.",
    )


def test_digest_tracks_directory_content_and_recipe_contract(tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    module = source / "server.py"
    module.write_text("VALUE = 1\n")
    recipe = _recipe()
    original = validation._validation_input_digest(tmp_path, recipe)

    module.write_text("VALUE = 2\n")
    assert validation._validation_input_digest(tmp_path, recipe) != original
    assert (
        validation._validation_input_digest(
            tmp_path, recipe.model_copy(update={"command": "python -m pytest tests/test_api.py"})
        )
        != original
    )


def test_digest_rejects_missing_and_symlinked_inputs(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        validation._validation_input_digest(tmp_path, _recipe())

    (tmp_path / "src").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="must not be a symlink"):
        validation._validation_input_digest(tmp_path, _recipe())


def test_recipe_artifact_must_be_inside_workspace_and_match_schema(tmp_path: Path) -> None:
    artifact = tmp_path / "recipes.json"
    recipe = _recipe(input_paths=["server.py"])
    artifact.write_text(ValidationRecipeArtifact(recipes=[recipe]).model_dump_json())
    assert validation._load_validation_recipes(tmp_path, "recipes.json") == [recipe]

    artifact.write_text("{")
    with pytest.raises(ValueError, match="not valid JSON"):
        validation._load_validation_recipes(tmp_path, "recipes.json")
    artifact.write_text(json.dumps({"version": 2, "recipes": [recipe.model_dump()]}))
    with pytest.raises(ValueError, match="does not match version 1"):
        validation._load_validation_recipes(tmp_path, "recipes.json")
    with pytest.raises(ValueError, match="escapes the workspace"):
        validation._load_validation_recipes(tmp_path, "../outside.json")


def test_reuse_selects_newest_matching_pass_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recipe = _recipe()
    digest = "same-inputs"
    old = tmp_path / "old.json"
    newest = tmp_path / "new.json"
    old_pass = FrameworkValidationResult(
        recipe=recipe, input_digest=digest, passed=True, output="old pass"
    )
    passing = FrameworkValidationResult(
        recipe=recipe, input_digest=digest, passed=True, output="new pass"
    )
    old.write_text(json.dumps({"results": [old_pass.model_dump(mode="json")]}))
    newest.write_text(
        json.dumps(
            {
                "results": [
                    FrameworkValidationResult(
                        recipe=recipe, input_digest=digest, passed=False
                    ).model_dump(mode="json"),
                    passing.model_dump(mode="json"),
                ]
            }
        )
    )
    monkeypatch.setattr(
        validation.issue_board, "validation_result_artifact_paths", lambda _path: [old, newest]
    )
    reused = validation._reusable_validation_result(tmp_path / "progress.md", recipe, digest)
    assert reused is not None
    assert reused.passed
    assert reused.reused
    assert reused.output == "new pass"
    assert (
        validation._reusable_validation_result(tmp_path / "progress.md", recipe, "changed") is None
    )
