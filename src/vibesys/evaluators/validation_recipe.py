"""Judge-approved local validation recipes: implementer-authored, framework-run.

``multi`` and ``profile_multi`` each have their own reuse/hashing logic over
these types (``loops/<strategy>/validation.py``); the types themselves live
here so neither strategy peer imports the other to get them.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ValidationRecipe(BaseModel):
    """A bounded, reusable local validation command proposed by an implementer.

    The independent judge audits the recipe before the framework executes it.
    Target, deployment, benchmark, profiler, and official evaluator commands
    belong to their existing framework-owned gates instead.
    """

    model_config = ConfigDict(extra="forbid")

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
    input_paths: list[str] = Field(
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
    def _validate_input_paths(cls, values: list[str]) -> list[str]:
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
        return normalized


class ValidationRecipeArtifact(BaseModel):
    """Versioned candidate-authored container for local validation recipes."""

    model_config = ConfigDict(
        extra="forbid",
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
    recipes: list[ValidationRecipe] = Field(min_length=1, max_length=8)


class FrameworkValidationResult(BaseModel):
    """Framework-owned result for one audited validation recipe."""

    model_config = ConfigDict(extra="forbid")

    recipe: ValidationRecipe
    input_digest: str
    passed: bool
    reused: bool = False
    exit_code: int | None = None
    output: str = ""
    error: str | None = None


__all__ = ["FrameworkValidationResult", "ValidationRecipe", "ValidationRecipeArtifact"]
