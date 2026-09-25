"""Judge-approved local recipe validation inputs and pass reuse."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

from vibesys.agent_run import issue_board
from vibesys.schemas import FrameworkValidationResult, ValidationRecipeArtifact

_MAX_INPUT_FILES = 4096
_MAX_INPUT_BYTES = 256 * 1024 * 1024

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.schemas import ValidationRecipe


def _validation_input_digest(workspace: Path, recipe: ValidationRecipe) -> str:
    """Hash the declared workspace inputs that determine recipe reuse."""
    digest = hashlib.sha256()
    workspace_root = workspace.resolve()
    total_files = 0
    total_bytes = 0
    for relative in sorted(recipe.input_paths):
        unresolved = workspace / relative
        if unresolved.is_symlink():
            message = f"validation input must not be a symlink: {relative}"
            raise ValueError(message)
        path = unresolved.resolve()
        if not path.is_relative_to(workspace_root):
            message = f"validation input escapes workspace: {relative}"
            raise ValueError(message)
        if not path.exists():
            message = f"validation input does not exist: {relative}"
            raise ValueError(message)
        digest.update(relative.encode("utf-8"))
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
            if total_files > _MAX_INPUT_FILES or total_bytes > _MAX_INPUT_BYTES:
                message = "validation inputs exceed the 4096-file/256-MiB reuse-hash limit"
                raise ValueError(message)
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
    for artifact in reversed(issue_board.validation_result_artifact_paths(progress_path)):
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
        message = "validation recipe artifact escapes the workspace"
        raise ValueError(message)
    if not path.is_file():
        message = f"validation recipe artifact does not exist: {artifact}"
        raise ValueError(message)
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        message = f"validation recipe artifact is not valid JSON: {exc}"
        raise ValueError(message) from exc
    try:
        return ValidationRecipeArtifact.model_validate(payload).recipes
    except (TypeError, ValueError) as exc:
        message = f"validation recipe artifact does not match version 1: {exc}"
        raise ValueError(message) from exc
