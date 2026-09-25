"""Judge-approved local recipe validation inputs and pass reuse."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from vibesys.evaluators.validation_recipe import (
    FrameworkValidationResult,
    ValidationRecipeArtifact,
)
from vibesys.orchestration import artifacts

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.evaluators.validation_recipe import ValidationRecipe


def _validation_input_digest(workspace: Path, recipe: ValidationRecipe) -> str:
    """Hash the declared workspace inputs that determine recipe reuse."""
    digest = hashlib.sha256()
    workspace_root = workspace.resolve()
    total_files = 0
    total_bytes = 0
    for relative in sorted(recipe.input_paths):
        unresolved = workspace / relative
        if unresolved.is_symlink():
            raise ValueError(f"validation input must not be a symlink: {relative}")  # noqa: TRY003  # tracked: #288
        path = unresolved.resolve()
        if not path.is_relative_to(workspace_root):
            raise ValueError(f"validation input escapes workspace: {relative}")  # noqa: TRY003  # tracked: #288
        if not path.exists():
            raise ValueError(f"validation input does not exist: {relative}")  # noqa: TRY003  # tracked: #288
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0dir\0" if path.is_dir() else b"\0file\0")
        entries = [path]
        if path.is_dir():
            entries = sorted(candidate for candidate in path.rglob("*") if candidate.is_file())
        for entry in entries:
            if entry.is_symlink():
                raise ValueError(f"validation input must not be a symlink: {relative}")  # noqa: TRY003  # tracked: #288
            total_files += 1
            total_bytes += entry.stat().st_size
            if total_files > 4096 or total_bytes > 256 * 1024 * 1024:  # noqa: PLR2004  # tracked: #288
                raise ValueError("validation inputs exceed the 4096-file/256-MiB reuse-hash limit")  # noqa: TRY003  # tracked: #288
            entry_relative = entry.relative_to(workspace_root).as_posix()
            digest.update(entry_relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(entry.read_bytes())
            digest.update(b"\0")
    digest.update(recipe.command.encode("utf-8"))
    digest.update(b"\0")
    digest.update(str(recipe.timeout_seconds).encode("ascii"))
    return digest.hexdigest()


def _reusable_validation_result(
    progress_path: Path,
    recipe: ValidationRecipe,
    input_digest: str,
) -> FrameworkValidationResult | None:
    """Return the newest matching framework PASS, if one exists."""
    for artifact in reversed(artifacts.validation_result_artifact_paths(progress_path)):
        try:
            payload = json.loads(artifact.read_text())
            results = payload.get("results", [])
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        for raw in reversed(results):
            try:
                result = FrameworkValidationResult.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if result.passed and result.input_digest == input_digest and result.recipe == recipe:
                return result.model_copy(update={"reused": True})
    return None


def _load_validation_recipes(workspace: Path, artifact: str) -> list[ValidationRecipe]:
    """Load and validate a candidate-authored recipe file inside the workspace."""
    workspace_root = workspace.resolve()
    path = (workspace / artifact).resolve()
    if not path.is_relative_to(workspace_root):
        raise ValueError("validation recipe artifact escapes the workspace")  # noqa: TRY003  # tracked: #288
    if not path.is_file():
        raise ValueError(f"validation recipe artifact does not exist: {artifact}")  # noqa: TRY003  # tracked: #288
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"validation recipe artifact is not valid JSON: {exc}") from exc  # noqa: TRY003  # tracked: #288
    try:
        return ValidationRecipeArtifact.model_validate(payload).recipes
    except (TypeError, ValueError) as exc:
        raise ValueError(f"validation recipe artifact does not match version 1: {exc}") from exc  # noqa: TRY003  # tracked: #288
