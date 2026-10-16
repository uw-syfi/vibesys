"""Trusted local-validation recipe execution, reusing only passes the runtime recorded.

The recipes and the report live in the agent's workspace: the recipes are read through
``_workspace_files`` as a validated model, and the report is written there for the agent
to read. Whether a recipe may be skipped is a trust decision, answered only by the
runtime's own ``ValidationPasses`` store, never by a file in the workspace.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    field_validator,
)

from vs_project.api.workspace_tree import WorkspacePathError, relative_parts
from vs_redaction.api import redact_diagnostic_tail
from vs_runtime._workspace_files import (
    WorkspaceFileProblem,
    WorkspaceFileProblemKind,
    hash_inputs,
    read_agent_model,
    write_for_agent,
)
from vs_runtime.contracts import WorkspaceAccess, validate_workspace_writable_paths

if TYPE_CHECKING:
    from pathlib import Path

    from vs_runtime._validation_passes import ValidationPasses
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
            "Workspace-relative source, test, lock, or configuration paths the "
            "command reads; they must exist and are hashed into the result."
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
    operator_output: str | None = Field(default=None, exclude=True)
    error: str | None = None
    operator_error: str | None = Field(default=None, exclude=True)


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


def _load_recipes(workspace: Path, artifact: str) -> tuple[ValidationRecipe, ...]:
    loaded = read_agent_model(workspace, artifact, ValidationRecipeArtifact)
    if isinstance(loaded, WorkspaceFileProblem):
        raise LocalValidationRecipeError(
            LocalValidationRecipeErrorKind.INVALID_ARTIFACT, _recipe_problem(loaded)
        )
    recipes = loaded.recipes
    names = [recipe.name for recipe in recipes]
    if len(names) != len(set(names)):
        raise LocalValidationRecipeError(
            LocalValidationRecipeErrorKind.DUPLICATE_NAMES,
            "validation recipes contain duplicate names",
        )
    return recipes


def _recipe_problem(problem: WorkspaceFileProblem) -> str:
    match problem.kind:
        case WorkspaceFileProblemKind.ESCAPES:
            return "validation recipe artifact escapes the workspace"
        case WorkspaceFileProblemKind.MISSING:
            return f"validation recipe artifact does not exist: {problem.relative_path}"
        case WorkspaceFileProblemKind.NOT_READABLE:
            return f"validation recipe artifact cannot be read: {problem.detail}"
        case WorkspaceFileProblemKind.TOO_LARGE:
            return f"validation recipe artifact {problem.detail}"
        case WorkspaceFileProblemKind.NOT_JSON:
            return f"validation recipe artifact is not valid JSON: {problem.detail}"
        case WorkspaceFileProblemKind.INVALID:
            return f"validation recipe artifact does not match version 1: {problem.detail}"


def _input_digest(workspace: Path, recipe: ValidationRecipe, revision: str) -> str:
    """Identify what a pass vouches for: the whole tree at ``revision``, not the declared inputs.

    The agent chooses ``recipe.input_paths``, so they cannot bound what a pass covers: a
    candidate that edited a file it left undeclared would otherwise reuse another's pass.
    The runtime's own snapshot revision names the whole tree; the declared inputs are
    still hashed so an unreadable or oversized input fails the recipe.
    """
    inputs = hash_inputs(
        workspace,
        recipe.input_paths,
        max_files=_MAX_INPUT_FILES,
        max_bytes=_MAX_INPUT_BYTES,
    )
    digest = hashlib.sha256(inputs.encode())
    digest.update(b"\0")
    digest.update(revision.encode())
    digest.update(b"\0")
    digest.update(recipe.command.encode())
    digest.update(b"\0")
    digest.update(str(recipe.timeout_seconds).encode("ascii"))
    return digest.hexdigest()


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
        operator_output = execution.diagnostic_output.strip()
        result = FrameworkValidationResult(
            recipe=recipe,
            input_digest=input_digest,
            passed=execution.exit_code == 0,
            exit_code=execution.exit_code,
            output=output[-_OUTPUT_TAIL_CHARS:],
            operator_output=operator_output[-_OUTPUT_TAIL_CHARS:],
            error=None if execution.exit_code == 0 else "command exited nonzero",
            operator_error=None if execution.exit_code == 0 else "command exited nonzero",
        )
    except Exception as error:  # noqa: BLE001  # lint-waiver: LW-732841 [BLE001]; trusted command failures are validation results, while cancellation remains a BaseException.
        error_text = f"command could not be executed: {error}"
        result = FrameworkValidationResult(
            recipe=recipe,
            input_digest=input_digest,
            passed=False,
            error=error_text,
            operator_error=redact_diagnostic_tail(error_text, max_chars=_OUTPUT_TAIL_CHARS),
        )
    changes = await workspace.pending_changes()
    if not changes:
        return result, False
    mutation = f"validation command mutated the workspace: {', '.join(changes[:8])}"
    return (
        result.model_copy(
            update={
                "passed": False,
                "error": mutation,
                "operator_error": redact_diagnostic_tail(mutation, max_chars=_OUTPUT_TAIL_CHARS),
            }
        ),
        True,
    )


def _write_report(workspace: Path, location: str, results: list[FrameworkValidationResult]) -> None:
    payload = json.dumps(
        {"version": 1, "results": [item.model_dump(mode="json") for item in results]},
        indent=2,
    )
    write_for_agent(workspace, location, f"{payload}\n".encode())


class LocalValidator:
    """Run trusted validation recipes, skipping only passes the runtime itself recorded."""

    def __init__(self, commands: Commands, passes: ValidationPasses) -> None:
        """Run recipes through ``commands``; remember and reuse passes in ``passes``.

        ``passes`` must live outside every workspace: it is the only evidence a recipe
        already passed.
        """
        self._commands = commands
        self._passes = passes

    async def run(
        self,
        workspace: Workspace,
        *,
        recipe_artifact: str,
        report_location: str,
        events: LocalValidationEvents | None = None,
    ) -> tuple[FrameworkValidationResult, ...]:
        """Parse, execute, isolate and report the recipes the agent wrote.

        A recipe is skipped only when the pass store holds a pass of exactly it over
        the same workspace revision (the whole tree, whatever the recipe declares); every new
        pass is recorded there. The report written at ``report_location`` is for the agent to
        read and is never read back.
        """
        recipes = _load_recipes(workspace.path, recipe_artifact)
        try:
            relative_parts(report_location)
        except WorkspacePathError as error:
            raise LocalValidationRecipeError(
                LocalValidationRecipeErrorKind.INVALID_ARTIFACT,
                "local validation report location escapes the workspace",
            ) from error
        sink = events or _NoLocalValidationEvents()
        results: list[FrameworkValidationResult] = []
        restore_required = False
        completed = False
        entry_revision = await workspace.snapshot("framework-local-validation-input")
        try:
            for recipe in recipes:
                try:
                    digest = _input_digest(workspace.path, recipe, entry_revision)
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
                recorded = self._passes.find(recipe, digest)
                if recorded is not None:
                    reused = recorded.model_copy(update={"reused": True})
                    results.append(reused)
                    sink.finished(reused)
                    continue
                result, mutated = await _execute_recipe(self._commands, workspace, recipe, digest)
                restore_required = restore_required or mutated
                if result.passed:
                    self._passes.record(result)
                results.append(result)
                sink.finished(result)
                if not result.passed:
                    break
            completed = True
        finally:
            if restore_required or not completed:
                await workspace.restore(entry_revision, clean=True)

        _write_report(workspace.path, report_location, results)
        await workspace.snapshot("framework-local-validation")
        return tuple(results)


__all__ = [
    "VALIDATION_RECIPE_ARTIFACT_DESCRIPTION",
    "FrameworkValidationResult",
    "LocalValidationEvents",
    "LocalValidationRecipeError",
    "LocalValidationRecipeErrorKind",
    "LocalValidator",
    "ValidationRecipe",
    "ValidationRecipeArtifact",
    "ValidationRecipeArtifactPath",
    "check_recipe_artifact_path",
]
