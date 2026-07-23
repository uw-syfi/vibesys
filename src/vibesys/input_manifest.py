"""Command-based target input manifests."""

from __future__ import annotations

import shlex
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from vibesys.constants import PROJECT_ROOT
from vibesys.domains.base import DomainName

MANIFEST_NAME = "vibesys.input.toml"


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

    seed: str | None = None
    sources: tuple[WorkspaceSource, ...] = ()

    @field_validator("seed")
    @classmethod
    def _relative_seed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            raise ValueError("seed must be a non-empty path")
        if Path(value).is_absolute():
            raise ValueError("seed must be relative to the input bundle")
        return value


class WorkspaceSource(BaseModel):
    """Pinned git source materialized into the mutable candidate workspace."""

    model_config = ConfigDict(extra="forbid")

    name: str
    repo: str
    commit: str
    dest: str
    strip_git: bool = True

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if not value:
            raise ValueError("name must be non-empty")
        if any(character.isspace() for character in value):
            raise ValueError("name must not contain whitespace")
        return value

    @field_validator("repo")
    @classmethod
    def _valid_repo(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("repo must be non-empty")
        parsed = urlparse(value)
        if parsed.scheme and parsed.scheme not in {"file", "http", "https", "ssh", "git"}:
            raise ValueError(f"unsupported repo URL scheme: {parsed.scheme}")
        return value

    @field_validator("commit")
    @classmethod
    def _valid_commit(cls, value: str) -> str:
        if not value:
            raise ValueError("commit must be non-empty")
        if not (7 <= len(value) <= 64) or any(c not in "0123456789abcdefABCDEF" for c in value):
            raise ValueError("commit must be a 7-64 character hexadecimal hash")
        return value.lower()

    @field_validator("dest")
    @classmethod
    def _relative_dest(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("dest must be a non-empty path")
        path = Path(value)
        if path.is_absolute():
            raise ValueError("dest must be relative to the workspace")
        if any(part in {"", ".", ".."} for part in path.parts):
            raise ValueError("dest must not contain empty, current, or parent path components")
        return value


class EvaluatorInput(BaseModel):
    """Trusted evaluator source copied into a fresh candidate workspace."""

    model_config = ConfigDict(extra="forbid")

    source: str

    @field_validator("source")
    @classmethod
    def _relative_source(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source must be a non-empty path")
        if Path(value).is_absolute():
            raise ValueError("source must be relative to the input bundle")
        return value


class BenchmarkResult(BaseModel):
    """Machine-readable scalar result emitted by a benchmark command."""

    model_config = ConfigDict(extra="forbid")

    json_argument: str
    metric: str

    @field_validator("json_argument")
    @classmethod
    def _single_option(cls, value: str) -> str:
        if not value.startswith("-") or any(character.isspace() for character in value):
            raise ValueError("json_argument must be one option-style argv element")
        return value

    @field_validator("metric")
    @classmethod
    def _metric_name(cls, value: str) -> str:
        if not value or any(character.isspace() for character in value):
            raise ValueError("metric must be a non-empty JSON field name without whitespace")
        return value


class BenchmarkCommand(InputCommand):
    """Benchmark command with an optional trusted scalar-result contract."""

    result: BenchmarkResult | None = None


class AgentInput(BaseModel):
    """Agent-loop metadata declared by an input bundle."""

    model_config = ConfigDict(extra="forbid")

    domain: DomainName


class InputManifest(BaseModel):
    """Versioned evaluator-command manifest for an input bundle."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    agent: AgentInput
    accuracy: InputCommand
    benchmark: BenchmarkCommand
    workspace: WorkspaceInput | None = None
    evaluator: EvaluatorInput | None = None

    @model_validator(mode="after")
    def _unique_workspace_source_destinations(self) -> InputManifest:
        if self.workspace is None:
            return self
        seen_names: set[str] = set()
        seen_dests: set[str] = set()
        for source in self.workspace.sources:
            if source.name in seen_names:
                raise ValueError(f"duplicate workspace source name: {source.name}")
            if source.dest in seen_dests:
                raise ValueError(f"duplicate workspace source destination: {source.dest}")
            seen_names.add(source.name)
            seen_dests.add(source.dest)
        return self


class InputBundle(BaseModel):
    """Resolved input bundle with manifest commands and conventional files."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    manifest_path: Path
    objective_path: Path
    reference_path: Path | None
    workspace_seed_path: Path | None
    evaluator_path: Path | None
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
    def domain(self) -> DomainName:
        return self.manifest.agent.domain

    @property
    def accuracy_command_display(self) -> str:
        return self.manifest.accuracy.display()

    @property
    def benchmark_command_display(self) -> str:
        return self.manifest.benchmark.display()

    @property
    def benchmark_result(self) -> BenchmarkResult | None:
        return self.manifest.benchmark.result

    @property
    def workspace_sources(self) -> tuple[WorkspaceSource, ...]:
        if self.manifest.workspace is None:
            return ()
        return self.manifest.workspace.sources


def load_input_bundle(
    path: Path,
    *,
    project_root: Path | None = None,
    allow_materialized_sources: bool = False,
) -> InputBundle:
    """Load and validate a command-based input bundle.

    ``allow_materialized_sources`` is used only when resuming an experiment
    from its copied workspace. Starter and evaluator sources may have lived
    outside the original bundle and are no longer needed once materialized.
    """

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
    if (
        manifest.workspace is not None
        and manifest.workspace.seed is not None
        and not allow_materialized_sources
    ):
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

    evaluator_path = None
    if manifest.evaluator is not None and not allow_materialized_sources:
        evaluators_root = (project_root / "examples" / "evaluators").resolve()
        evaluator_path = (root / manifest.evaluator.source).resolve()
        try:
            evaluator_path.relative_to(evaluators_root)
        except ValueError as exc:
            raise ValueError(
                f"evaluator.source must resolve inside {evaluators_root}: "
                f"{manifest.evaluator.source}"
            ) from exc
        if not evaluator_path.exists():
            raise FileNotFoundError(f"evaluator.source path does not exist: {evaluator_path}")
        if not evaluator_path.is_dir():
            raise ValueError(f"evaluator.source path is not a directory: {evaluator_path}")

    return InputBundle(
        root=root,
        manifest_path=manifest_path,
        objective_path=objective_path,
        reference_path=reference_path,
        workspace_seed_path=workspace_seed_path,
        evaluator_path=evaluator_path,
        manifest=manifest,
    )
