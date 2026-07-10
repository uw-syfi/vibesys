"""Command-based target input manifests."""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from vibe_serve.constants import PROJECT_ROOT

MANIFEST_NAME = "vibeserve.input.toml"


class InputCommand(BaseModel):
    """One evaluator command declared by an input bundle."""

    model_config = ConfigDict(extra="forbid")

    command: tuple[str, ...]
    timeout_seconds: int | None = Field(default=None, gt=0)

    @field_validator("command")
    @classmethod
    def _non_empty_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("command must contain at least one argv element")
        if any(not part for part in value):
            raise ValueError("command elements must be non-empty strings")
        return value

    def display(self) -> str:
        return " ".join(shlex.quote(part) for part in self.command)


class WorkspaceInput(BaseModel):
    """Optional starter content copied into a fresh candidate workspace."""

    model_config = ConfigDict(extra="forbid")

    seed: str

    @field_validator("seed")
    @classmethod
    def _relative_seed(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("seed must be a non-empty path")
        if Path(value).is_absolute():
            raise ValueError("seed must be relative to the input bundle")
        return value


class InputManifest(BaseModel):
    """Versioned evaluator-command manifest for an input bundle."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    accuracy: InputCommand
    benchmark: InputCommand
    workspace: WorkspaceInput | None = None


class InputBundle(BaseModel):
    """Resolved input bundle with manifest commands and conventional files."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    manifest_path: Path
    objective_path: Path
    reference_path: Path | None
    workspace_seed_path: Path | None
    manifest: InputManifest

    @property
    def objective(self) -> str:
        return self.objective_path.read_text()

    @property
    def accuracy_command(self) -> tuple[str, ...]:
        return self.manifest.accuracy.command

    @property
    def benchmark_command(self) -> tuple[str, ...]:
        return self.manifest.benchmark.command

    @property
    def accuracy_command_display(self) -> str:
        return self.manifest.accuracy.display()

    @property
    def benchmark_command_display(self) -> str:
        return self.manifest.benchmark.display()


def load_input_bundle(path: Path, *, project_root: Path | None = None) -> InputBundle:
    """Load and validate a command-based input bundle."""

    project_root = (project_root or PROJECT_ROOT).resolve()
    root = path.expanduser().resolve()
    if not root.exists():
        raise FileNotFoundError(f"--input path does not exist: {path}")
    if not root.is_dir():
        raise ValueError(f"--input path is not a directory: {path}")

    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Input manifest not found: {manifest_path}")

    objective_path = root / "OBJECTIVE.md"
    if not objective_path.is_file():
        raise FileNotFoundError(f"OBJECTIVE.md not found: {objective_path}")

    try:
        manifest = InputManifest.model_validate(tomllib.loads(manifest_path.read_text()))
    except ValidationError as exc:
        raise ValueError(f"Invalid input manifest {manifest_path}: {exc}") from exc

    for label, command in (
        ("accuracy.command", manifest.accuracy.command),
        ("benchmark.command", manifest.benchmark.command),
    ):
        executable = Path(command[0])
        if executable.is_absolute():
            raise ValueError(
                f"{label} executable must be relative to the input bundle: {command[0]}"
            )
        if "/" not in command[0]:
            continue
        resolved = (root / executable).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} executable escapes the input bundle: {command[0]}") from exc
        if not resolved.exists():
            raise FileNotFoundError(f"{label} executable does not exist: {resolved}")
        if not resolved.is_file():
            raise ValueError(f"{label} executable is not a file: {resolved}")

    reference_path = root / "reference"
    if reference_path.exists() and not reference_path.is_dir():
        raise ValueError(f"reference path is not a directory: {reference_path}")
    if not reference_path.exists():
        reference_path = None

    workspace_seed_path = None
    if manifest.workspace is not None:
        starters_root = (project_root / "examples" / "starters").resolve()
        workspace_seed_path = (root / manifest.workspace.seed).resolve()
        try:
            workspace_seed_path.relative_to(starters_root)
        except ValueError as exc:
            raise ValueError(
                f"workspace.seed must resolve inside {starters_root}: {manifest.workspace.seed}"
            ) from exc
        if not workspace_seed_path.exists():
            raise FileNotFoundError(f"workspace.seed path does not exist: {workspace_seed_path}")
        if not workspace_seed_path.is_dir():
            raise ValueError(f"workspace.seed path is not a directory: {workspace_seed_path}")

    return InputBundle(
        root=root,
        manifest_path=manifest_path,
        objective_path=objective_path,
        reference_path=reference_path,
        workspace_seed_path=workspace_seed_path,
        manifest=manifest,
    )
