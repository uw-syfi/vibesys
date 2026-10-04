"""Trusted local-validation recipe execution and durable result reuse."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
)

from vs_project.api import atomic_write_bytes
from vs_runtime.contracts import WorkspaceAccess, validate_workspace_writable_paths

if TYPE_CHECKING:
    from vs_runtime.contracts import Commands, Workspace

_MAX_INPUT_FILES = 4096
_MAX_INPUT_BYTES = 256 * 1024 * 1024
_OUTPUT_TAIL_CHARS = 8000
_RECIPE_EXCERPT_CHARS = 80

VALIDATION_RECIPE_ARTIFACT_DESCRIPTION = (
    "Workspace-relative path of a JSON file holding local validation recipes, "
    "ending in .json and containing no whitespace. Never a description or inline "
    "JSON. Omit the field when there are no recipes."
)

# One definition of the agent-reported recipe path, used both as the JSON-schema
# constraint the agent's structured reply is checked against (so a violation is
# reported inside the agent's own turn) and by the runtime before any file read.
ValidationRecipeArtifactPath = Annotated[
    str,
    StringConstraints(min_length=1, max_length=512, pattern=r"^\S+\.json$"),
]


_RECIPE_PATH_ADAPTER = TypeAdapter(ValidationRecipeArtifactPath)


class ValidationRecipe(BaseModel):
    """One bounded trusted command proposed by an agent and approved by policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(
        min_length=1,
        max_length=80,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
        description="Stable short identifier for this validation recipe.",
    )
    command: str = Field(
        min_length=1,
        max_length=4000,
        description="Exact non-interactive command to execute from the workspace root.",
    )
    input_paths: tuple[str, ...] = Field(
        min_length=1,
        max_length=64,
        description=(
            "Workspace-relative source, test, lock, or configuration paths that "
            "fully determine whether a prior passing result can be reused."
        ),
    )
    timeout_seconds: int = Field(
        default=300,
        ge=1,
        le=1800,
        description="Hard wall-clock timeout for this local validation command.",
    )
    purpose: str = Field(
        min_length=1,
        max_length=500,
        description="The observable contract this command validates.",
    )

    @field_validator("command", "purpose")
    @classmethod
    def _strip_recipe_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            message = "must contain non-whitespace text"
            raise ValueError(message)
        return value

    @field_validator("input_paths")
    @classmethod
    def _validate_input_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for raw in values:
            value = raw.strip()
            path = PurePosixPath(value)
            if not value or path.is_absolute() or value == "." or ".." in path.parts:
                message = (
                    "input_paths must contain non-empty workspace-relative paths "
                    "without parent traversal"
                )
                raise ValueError(message)
            normalized.append(path.as_posix())
        if len(set(normalized)) != len(normalized):
            message = "input_paths must not contain duplicates"
            raise ValueError(message)
        return tuple(normalized)


class ValidationRecipeArtifact(BaseModel):
    """Versioned candidate-authored container for local validation recipes."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        json_schema_extra={
            "examples": [
                {
                    "version": 1,
                    "recipes": [
                        {
                            "name": "focused-tests",
                            "command": "uv run pytest -q test_server.py",
                            "input_paths": [
                                "server.py",
                                "test_server.py",
                                "pyproject.toml",
                                "uv.lock",
                            ],
                            "timeout_seconds": 300,
                            "purpose": "Exercise the focused local server contract.",
                        }
                    ],
                }
            ]
        },
    )

    version: Literal[1] = 1
    recipes: tuple[ValidationRecipe, ...] = Field(min_length=1, max_length=8)


class FrameworkValidationResult(BaseModel):
    """Runtime-owned result for one trusted local-validation recipe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe: ValidationRecipe
    input_digest: str
    passed: bool
    reused: bool = False
    exit_code: int | None = None
    output: str = ""
    error: str | None = None


class LocalValidationRecipeErrorKind(StrEnum):
    """Stable category for product-owned validation error interpretation."""

    INVALID_ARTIFACT = "invalid_artifact"
    DUPLICATE_NAMES = "duplicate_names"


class LocalValidationRecipeError(ValueError):
    """A local-validation recipe artifact or destination is invalid."""

    def __init__(self, kind: LocalValidationRecipeErrorKind, detail: str) -> None:
        self.kind = kind
        super().__init__(detail)


class LocalValidationEvents(Protocol):
    """Receive policy-neutral recipe lifecycle observations."""

    def started(self, recipe: ValidationRecipe) -> None:
        """Observe a recipe immediately before reuse or execution."""
        ...

    def finished(self, result: FrameworkValidationResult) -> None:
        """Observe the durable result of one recipe."""
        ...


class _NoLocalValidationEvents:
    def started(self, recipe: ValidationRecipe) -> None:
        del recipe

    def finished(self, result: FrameworkValidationResult) -> None:
        del result


def check_recipe_artifact_path(artifact: str) -> None:
    """Reject an agent-reported recipe reference that cannot name a recipe file.

    Raises:
        LocalValidationRecipeError: The value is prose, inline JSON, a path with
            whitespace, not a ``.json`` file, or not a canonical workspace path.
    """
    try:
        _RECIPE_PATH_ADAPTER.validate_python(artifact)
        validate_workspace_writable_paths(WorkspaceAccess.LIMITED, (artifact,))
    except ValueError as error:
        excerpt = artifact[:_RECIPE_EXCERPT_CHARS]
        if len(artifact) > _RECIPE_EXCERPT_CHARS:
            excerpt += "..."
        message = (
            "validation recipe artifact must be a canonical workspace-relative path to a "
            ".json file with no whitespace, not a description or inline content; write the "
            f"recipes to a file and report only its path: {excerpt!r}"
        )
        raise LocalValidationRecipeError(
            LocalValidationRecipeErrorKind.INVALID_ARTIFACT, message
        ) from error


def _workspace_path(workspace: Path, relative: str, *, kind: str) -> Path:
    root = workspace.resolve()
    path = (workspace / relative).resolve()
    if not path.is_relative_to(root):
        raise LocalValidationRecipeError(
            LocalValidationRecipeErrorKind.INVALID_ARTIFACT,
            f"{kind} escapes the workspace",
        )
    return path


def _load_recipes(workspace: Path, artifact: str) -> tuple[ValidationRecipe, ...]:
    path = _workspace_path(workspace, artifact, kind="validation recipe artifact")
    if not path.is_file():
        raise LocalValidationRecipeError(
            LocalValidationRecipeErrorKind.INVALID_ARTIFACT,
            f"validation recipe artifact does not exist: {artifact}",
        )
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalValidationRecipeError(
            LocalValidationRecipeErrorKind.INVALID_ARTIFACT,
            f"validation recipe artifact is not valid JSON: {exc}",
        ) from exc
    try:
        recipes = ValidationRecipeArtifact.model_validate(payload).recipes
    except (TypeError, ValueError) as exc:
        raise LocalValidationRecipeError(
            LocalValidationRecipeErrorKind.INVALID_ARTIFACT,
            f"validation recipe artifact does not match version 1: {exc}",
        ) from exc
    names = [recipe.name for recipe in recipes]
    if len(names) != len(set(names)):
        raise LocalValidationRecipeError(
            LocalValidationRecipeErrorKind.DUPLICATE_NAMES,
            "validation recipes contain duplicate names",
        )
    return recipes


def _input_digest(workspace: Path, recipe: ValidationRecipe) -> str:
    digest = hashlib.sha256()
    root = workspace.resolve()
    total_files = 0
    total_bytes = 0
    for relative in sorted(recipe.input_paths):
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
            if total_files > _MAX_INPUT_FILES or total_bytes > _MAX_INPUT_BYTES:
                message = "validation inputs exceed the 4096-file/256-MiB reuse-hash limit"
                raise ValueError(message)
            digest.update(entry.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            digest.update(entry.read_bytes())
            digest.update(b"\0")
    digest.update(recipe.command.encode())
    digest.update(b"\0")
    digest.update(str(recipe.timeout_seconds).encode("ascii"))
    return digest.hexdigest()


def _reusable_result(
    report: Path,
    recipe: ValidationRecipe,
    input_digest: str,
) -> FrameworkValidationResult | None:
    if not report.parent.exists():
        return None
    for artifact in sorted(report.parent.glob("*.json"), reverse=True):
        if artifact == report:
            continue
        try:
            payload = json.loads(artifact.read_text())
            raw_results = payload.get("results", [])
        except (OSError, json.JSONDecodeError, AttributeError):
            continue
        for raw in reversed(raw_results):
            try:
                result = FrameworkValidationResult.model_validate(raw)
            except (TypeError, ValueError):
                continue
            if result.passed and result.input_digest == input_digest and result.recipe == recipe:
                return result.model_copy(update={"reused": True})
    return None


async def _execute_recipe(
    commands: Commands,
    workspace: Workspace,
    recipe: ValidationRecipe,
    input_digest: str,
) -> tuple[FrameworkValidationResult, bool]:
    try:
        execution = await commands.run_trusted_shell(
            recipe.command,
            workspace=workspace,
            timeout_seconds=recipe.timeout_seconds,
        )
        output = execution.output.strip()
        result = FrameworkValidationResult(
            recipe=recipe,
            input_digest=input_digest,
            passed=execution.exit_code == 0,
            exit_code=execution.exit_code,
            output=output[-_OUTPUT_TAIL_CHARS:],
            error=None if execution.exit_code == 0 else "command exited nonzero",
        )
    except Exception as error:  # noqa: BLE001  # lint-waiver: LW-732841 [BLE001]; trusted command failures are validation results, while cancellation remains a BaseException.
        result = FrameworkValidationResult(
            recipe=recipe,
            input_digest=input_digest,
            passed=False,
            error=f"command could not be executed: {error}",
        )
    changes = await workspace.pending_changes()
    if not changes:
        return result, False
    return (
        result.model_copy(
            update={
                "passed": False,
                "error": f"validation command mutated the workspace: {', '.join(changes[:8])}",
            }
        ),
        True,
    )


def _write_report(path: Path, results: list[FrameworkValidationResult]) -> None:
    payload = json.dumps(
        {"version": 1, "results": [item.model_dump(mode="json") for item in results]},
        indent=2,
    )
    atomic_write_bytes(path, f"{payload}\n".encode())


async def run_local_validation(
    commands: Commands,
    workspace: Workspace,
    *,
    recipe_artifact: str,
    report_location: str,
    events: LocalValidationEvents | None = None,
) -> tuple[FrameworkValidationResult, ...]:
    """Parse, execute, reuse, isolate, and persist trusted validation recipes."""
    recipes = _load_recipes(workspace.path, recipe_artifact)
    report = _workspace_path(
        workspace.path,
        report_location,
        kind="local validation report location",
    )
    sink = events or _NoLocalValidationEvents()
    results: list[FrameworkValidationResult] = []
    restore_required = False
    completed = False
    entry_revision = await workspace.snapshot("framework-local-validation-input")
    try:
        for recipe in recipes:
            try:
                digest = _input_digest(workspace.path, recipe)
            except (OSError, ValueError) as error:
                results.append(
                    FrameworkValidationResult(
                        recipe=recipe,
                        input_digest="",
                        passed=False,
                        error=str(error),
                    )
                )
                break
            sink.started(recipe)
            reused = _reusable_result(report, recipe, digest)
            if reused is not None:
                results.append(reused)
                sink.finished(reused)
                continue
            result, mutated = await _execute_recipe(commands, workspace, recipe, digest)
            restore_required = restore_required or mutated
            results.append(result)
            sink.finished(result)
            if not result.passed:
                break
        completed = True
    finally:
        if restore_required or not completed:
            await workspace.restore(entry_revision, clean=True)

    _write_report(report, results)
    await workspace.snapshot("framework-local-validation")
    return tuple(results)


__all__ = [
    "VALIDATION_RECIPE_ARTIFACT_DESCRIPTION",
    "FrameworkValidationResult",
    "LocalValidationEvents",
    "LocalValidationRecipeError",
    "LocalValidationRecipeErrorKind",
    "ValidationRecipe",
    "ValidationRecipeArtifact",
    "ValidationRecipeArtifactPath",
    "check_recipe_artifact_path",
    "run_local_validation",
]
