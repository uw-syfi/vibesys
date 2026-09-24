"""Command-based target input manifests."""

from __future__ import annotations

import json
import shlex
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from vibesys.constants import DomainName
from vibesys.evaluators.packages import (
    EvaluatorPackageRequirement,
    resolve_evaluator_package,
)
from vs_project.api import RunResourceRequest

if TYPE_CHECKING:
    from vibesys.evaluators.packages import ResolvedEvaluatorPackage
    from vs_project.api import Project, TaskDirectory

MANIFEST_NAME = "vibesys.input.toml"
_MIN_COMMIT_HASH_LENGTH = 7
_MAX_COMMIT_HASH_LENGTH = 64


class InputCommand(BaseModel):
    """One evaluator command declared by an input bundle."""

    model_config = ConfigDict(extra="forbid")

    command: tuple[str, ...] | None = None
    entrypoint: str | None = None
    args: tuple[str, ...] = ()
    timeout_seconds: int | None = Field(default=None, gt=0)

    @field_validator("command")
    @classmethod
    def _non_empty_command(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and not value:
            message = "command must contain at least one argv element"
            raise ValueError(message)
        if value is not None and any(not part for part in value):
            message = "command elements must be non-empty strings"
            raise ValueError(message)
        return value

    @field_validator("entrypoint")
    @classmethod
    def _non_empty_entrypoint(cls, value: str | None) -> str | None:
        if value is not None and (not value or any(character.isspace() for character in value)):
            message = "entrypoint must be a non-empty name without whitespace"
            raise ValueError(message)
        return value

    @field_validator("args")
    @classmethod
    def _non_empty_arguments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not part for part in value):
            message = "args elements must be non-empty strings"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _one_command_source(self) -> InputCommand:
        if (self.command is None) == (self.entrypoint is None):
            message = "declare exactly one of command or entrypoint"
            raise ValueError(message)
        if self.command is not None and self.args:
            message = "args may only be used with an evaluator entrypoint"
            raise ValueError(message)
        return self

    def display(self, *, resolved_command: tuple[str, ...] | None = None) -> str:
        """Render the resolved argv used by the evaluator."""
        command = resolved_command or self.command
        if command is None:
            message = "entrypoint commands must be resolved before display"
            raise ValueError(message)
        return " ".join(shlex.quote(part) for part in command)


class WorkspaceInput(BaseModel):
    """Pinned source repositories copied into a fresh candidate workspace."""

    model_config = ConfigDict(extra="forbid")

    sources: tuple[WorkspaceSource, ...] = ()


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
            message = "name must be non-empty"
            raise ValueError(message)
        if any(character.isspace() for character in value):
            message = "name must not contain whitespace"
            raise ValueError(message)
        return value

    @field_validator("repo")
    @classmethod
    def _valid_repo(cls, value: str) -> str:
        if not value.strip():
            message = "repo must be non-empty"
            raise ValueError(message)
        parsed = urlparse(value)
        if parsed.scheme and parsed.scheme not in {"file", "http", "https", "ssh", "git"}:
            message = f"unsupported repo URL scheme: {parsed.scheme}"
            raise ValueError(message)
        return value

    @field_validator("commit")
    @classmethod
    def _valid_commit(cls, value: str) -> str:
        if not value:
            message = "commit must be non-empty"
            raise ValueError(message)
        if not (_MIN_COMMIT_HASH_LENGTH <= len(value) <= _MAX_COMMIT_HASH_LENGTH) or any(
            c not in "0123456789abcdefABCDEF" for c in value
        ):
            message = "commit must be a 7-64 character hexadecimal hash"
            raise ValueError(message)
        return value.lower()

    @field_validator("dest")
    @classmethod
    def _relative_dest(cls, value: str) -> str:
        if not value.strip():
            message = "dest must be a non-empty path"
            raise ValueError(message)
        path = Path(value)
        if path.is_absolute():
            message = "dest must be relative to the workspace"
            raise ValueError(message)
        if any(part in {"", ".", ".."} for part in path.parts):
            message = "dest must not contain empty, current, or parent path components"
            raise ValueError(message)
        return value


class EvaluatorInput(BaseModel):
    """A legacy source tree or an exact reusable evaluator package."""

    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    name: str | None = None
    version: str | None = None

    @field_validator("source")
    @classmethod
    def _relative_source(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not value.strip():
            message = "source must be a non-empty path"
            raise ValueError(message)
        if Path(value).is_absolute():
            message = "source must be relative to the input bundle"
            raise ValueError(message)
        return value

    @model_validator(mode="after")
    def _one_evaluator_source(self) -> EvaluatorInput:
        package_values = (self.name, self.version)
        if self.source is not None:
            if any(value is not None for value in package_values):
                message = "evaluator source cannot be combined with name or version"
                raise ValueError(message)
            return self
        if self.name is None or self.version is None:
            message = "a packaged evaluator requires both name and version"
            raise ValueError(message)
        EvaluatorPackageRequirement(name=self.name, version=self.version)
        return self

    @property
    def package_requirement(self) -> EvaluatorPackageRequirement | None:
        """Return the exact package requirement, if this is not a legacy source."""
        if self.name is None or self.version is None:
            return None
        return EvaluatorPackageRequirement(name=self.name, version=self.version)


class BenchmarkResult(BaseModel):
    """Machine-readable scalar result emitted by a benchmark command."""

    model_config = ConfigDict(extra="forbid")

    json_argument: str
    metric: str

    @field_validator("json_argument")
    @classmethod
    def _single_option(cls, value: str) -> str:
        if not value.startswith("-") or any(character.isspace() for character in value):
            message = "json_argument must be one option-style argv element"
            raise ValueError(message)
        return value

    @field_validator("metric")
    @classmethod
    def _metric_name(cls, value: str) -> str:
        if not value or any(character.isspace() for character in value):
            message = "metric must be a non-empty JSON field name without whitespace"
            raise ValueError(message)
        return value


class BenchmarkCommand(InputCommand):
    """Benchmark command with an optional trusted result contract.

    A task declares at most one contract. ``[benchmark.result]`` scrapes one
    named scalar out of arbitrary benchmark JSON. ``result_protocol`` says the
    benchmark speaks the evaluator result protocol of that version and reports
    a complete validated metric row instead.
    """

    result: BenchmarkResult | None = None
    result_protocol: Literal[2] | None = None

    @model_validator(mode="after")
    def _one_result_contract(self) -> BenchmarkCommand:
        if self.result is not None and self.result_protocol is not None:
            message = (
                "benchmark.result and benchmark.result_protocol are mutually exclusive; "
                "declare only one"
            )
            raise ValueError(message)
        return self


class AgentInput(BaseModel):
    """Agent-loop metadata declared by an input bundle."""

    model_config = ConfigDict(extra="forbid")

    domain: DomainName


class ProfileGuidedInput(BaseModel):
    """Framework-owned component-attribution settings for profile-guided search."""

    model_config = ConfigDict(extra="forbid")

    command: tuple[str, ...]
    timeout_seconds: int = Field(default=1800, gt=0)
    result_protocol: Literal[1] = 1
    min_measured_rounds: int = Field(default=2, gt=0)
    min_relative_improvement: float = Field(default=0.02, ge=0)

    @field_validator("command")
    @classmethod
    def _non_empty_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            message = "command must contain at least one argv element"
            raise ValueError(message)
        if any(not part for part in value):
            message = "command elements must be non-empty strings"
            raise ValueError(message)
        return value


class ModalEnvironmentInput(BaseModel):
    """Task-owned settings for a Modal run environment."""

    model_config = ConfigDict(extra="forbid")

    entrypoint: str

    @field_validator("entrypoint")
    @classmethod
    def _relative_entrypoint(cls, value: str) -> str:
        if not value.strip():
            message = "entrypoint must be a non-empty path"
            raise ValueError(message)
        path = Path(value)
        if path.is_absolute():
            message = "entrypoint must be relative to the project root"
            raise ValueError(message)
        if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
            message = "entrypoint must not contain empty, current, or parent path components"
            raise ValueError(message)
        return value


class EnvironmentInput(BaseModel):
    """Optional task settings for concrete run environments."""

    model_config = ConfigDict(extra="forbid")

    modal: ModalEnvironmentInput | None = None


class InputManifest(BaseModel):
    """Versioned evaluator-command manifest for an input bundle."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    agent: AgentInput
    profile_guided: ProfileGuidedInput | None = None
    accuracy: InputCommand
    benchmark: BenchmarkCommand
    resources: RunResourceRequest | None = None
    environment: EnvironmentInput | None = None
    workspace: WorkspaceInput | None = None
    evaluator: EvaluatorInput | None = None

    @model_validator(mode="after")
    def _validate_cross_references(self) -> InputManifest:
        uses_entrypoint = any(
            command.entrypoint is not None for command in (self.accuracy, self.benchmark)
        )
        package = self.evaluator.package_requirement if self.evaluator is not None else None
        if uses_entrypoint and package is None:
            message = "evaluator entrypoints require a packaged [evaluator]"
            raise ValueError(message)
        if not uses_entrypoint and package is not None:
            message = "a packaged [evaluator] requires evaluator entrypoint commands"
            raise ValueError(message)
        if self.workspace is None:
            return self
        seen_names: set[str] = set()
        seen_dests: set[str] = set()
        for source in self.workspace.sources:
            if source.name in seen_names:
                message = f"duplicate workspace source name: {source.name}"
                raise ValueError(message)
            if source.dest in seen_dests:
                message = f"duplicate workspace source destination: {source.dest}"
                raise ValueError(message)
            seen_names.add(source.name)
            seen_dests.add(source.dest)
        return self


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def render_input_manifest(manifest: InputManifest) -> str:
    """Serialize a validated input manifest as deterministic TOML."""
    lines = [
        f"version = {manifest.version}",
        "",
        "[agent]",
        f"domain = {_toml_string(manifest.agent.domain.value)}",
    ]
    _append_profile_guided(lines, manifest)
    _append_accuracy(lines, manifest)
    _append_environment_and_resources(lines, manifest)
    _append_benchmark(lines, manifest)
    _append_workspace_sources(lines, manifest)
    _append_evaluator(lines, manifest)
    return "\n".join(lines) + "\n"


def _append_profile_guided(lines: list[str], manifest: InputManifest) -> None:
    if manifest.profile_guided is not None:
        lines.extend(
            [
                "",
                "[profile_guided]",
                f"command = {_toml_array(manifest.profile_guided.command)}",
                f"timeout_seconds = {manifest.profile_guided.timeout_seconds}",
                f"result_protocol = {manifest.profile_guided.result_protocol}",
                f"min_measured_rounds = {manifest.profile_guided.min_measured_rounds}",
                (f"min_relative_improvement = {manifest.profile_guided.min_relative_improvement}"),
            ]
        )


def _append_accuracy(lines: list[str], manifest: InputManifest) -> None:
    lines.extend(["", "[accuracy]"])
    if manifest.accuracy.command is not None:
        lines.append(f"command = {_toml_array(manifest.accuracy.command)}")
    else:
        accuracy_entrypoint = _required_manifest_text(
            manifest.accuracy.entrypoint,
            "accuracy.entrypoint",
        )
        lines.extend(
            [
                f"entrypoint = {_toml_string(accuracy_entrypoint)}",
                f"args = {_toml_array(manifest.accuracy.args)}",
            ]
        )
    if manifest.accuracy.timeout_seconds is not None:
        lines.append(f"timeout_seconds = {manifest.accuracy.timeout_seconds}")


def _append_environment_and_resources(lines: list[str], manifest: InputManifest) -> None:
    if manifest.environment is not None and manifest.environment.modal is not None:
        lines.extend(
            [
                "",
                "[environment.modal]",
                f"entrypoint = {_toml_string(manifest.environment.modal.entrypoint)}",
            ]
        )

    if manifest.resources is not None:
        lines.extend(
            [
                "",
                "[resources]",
                f"nodes = {manifest.resources.nodes}",
                f"accelerators_per_node = {manifest.resources.accelerators_per_node}",
                (f"accelerator_backend = {_toml_string(manifest.resources.accelerator_backend)}"),
            ]
        )
        if manifest.resources.cpus_per_node is not None:
            lines.append(f"cpus_per_node = {manifest.resources.cpus_per_node}")


def _append_benchmark(lines: list[str], manifest: InputManifest) -> None:
    lines.extend(
        [
            "",
            "[benchmark]",
        ]
    )
    if manifest.benchmark.command is not None:
        lines.append(f"command = {_toml_array(manifest.benchmark.command)}")
    else:
        benchmark_entrypoint = _required_manifest_text(
            manifest.benchmark.entrypoint,
            "benchmark.entrypoint",
        )
        lines.extend(
            [
                f"entrypoint = {_toml_string(benchmark_entrypoint)}",
                f"args = {_toml_array(manifest.benchmark.args)}",
            ]
        )
    if manifest.benchmark.timeout_seconds is not None:
        lines.append(f"timeout_seconds = {manifest.benchmark.timeout_seconds}")
    if manifest.benchmark.result is not None:
        lines.extend(
            [
                "",
                "[benchmark.result]",
                f"json_argument = {_toml_string(manifest.benchmark.result.json_argument)}",
                f"metric = {_toml_string(manifest.benchmark.result.metric)}",
            ]
        )


def _append_workspace_sources(lines: list[str], manifest: InputManifest) -> None:
    if manifest.workspace is not None:
        for source in manifest.workspace.sources:
            lines.extend(
                [
                    "",
                    "[[workspace.sources]]",
                    f"name = {_toml_string(source.name)}",
                    f"repo = {_toml_string(source.repo)}",
                    f"commit = {_toml_string(source.commit)}",
                    f"dest = {_toml_string(source.dest)}",
                    f"strip_git = {str(source.strip_git).lower()}",
                ]
            )


def _append_evaluator(lines: list[str], manifest: InputManifest) -> None:
    if manifest.evaluator is not None and manifest.evaluator.source is not None:
        lines.extend(
            [
                "",
                "[evaluator]",
                f"source = {_toml_string(manifest.evaluator.source)}",
            ]
        )
    elif manifest.evaluator is not None:
        evaluator_name = _required_manifest_text(manifest.evaluator.name, "evaluator.name")
        evaluator_version = _required_manifest_text(
            manifest.evaluator.version,
            "evaluator.version",
        )
        lines.extend(
            [
                "",
                "[evaluator]",
                f"name = {_toml_string(evaluator_name)}",
                f"version = {_toml_string(evaluator_version)}",
            ]
        )


def _required_manifest_text(value: str | None, field_name: str) -> str:
    """Recheck a required field that may have changed after model validation."""
    if not isinstance(value, str) or not value:
        message = f"{field_name} must be a non-empty string when rendering the manifest"
        raise ValueError(message)
    return value


class InputBundle(BaseModel):
    """Resolved input bundle with manifest commands and conventional files."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    root: Path
    task_root: Path
    task_name: str | None = None
    manifest_path: Path
    objective_path: Path
    dockerfile_path: Path | None
    reference_path: Path | None
    evaluator_path: Path | None
    evaluator_package_digest: str | None = None
    evaluator_package_root: Path | None = None
    resolved_accuracy_command: tuple[str, ...]
    resolved_benchmark_command: tuple[str, ...]
    manifest: InputManifest

    @property
    def objective(self) -> str:
        """Read the objective text from its declared file."""
        return self.objective_path.read_text()

    @property
    def accuracy_command(self) -> tuple[str, ...]:
        """Return the resolved accuracy evaluator argv."""
        return self.resolved_accuracy_command

    @property
    def benchmark_command(self) -> tuple[str, ...]:
        """Return the resolved benchmark evaluator argv."""
        return self.resolved_benchmark_command

    @property
    def domain(self) -> DomainName:
        """Return the domain declared by the input manifest."""
        return self.manifest.agent.domain

    @property
    def accuracy_command_display(self) -> str:
        """Render the configured accuracy command for user-facing output."""
        return self.manifest.accuracy.display(resolved_command=self.resolved_accuracy_command)

    @property
    def benchmark_command_display(self) -> str:
        """Render the configured benchmark command for user-facing output."""
        return self.manifest.benchmark.display(resolved_command=self.resolved_benchmark_command)

    @property
    def benchmark_result(self) -> BenchmarkResult | None:
        """Return the benchmark's declared result contract, if any."""
        return self.manifest.benchmark.result

    @property
    def benchmark_result_protocol(self) -> Literal[2] | None:
        """Return the evaluator result protocol version the benchmark speaks."""
        return self.manifest.benchmark.result_protocol

    @property
    def provisions_trace_telemetry(self) -> bool:
        """Whether the benchmark command captures a distributed trace graph.

        OTel profiling is only meaningful when the task itself provisions
        instrumentation and a collector and emits the two artifacts the
        profiler reads. A benchmark that names both ``--telemetry-output`` and
        ``--trace-graph-json`` is making exactly that declaration, so this is
        the capability signal ``auto`` profiler resolution keys off rather than
        a separate hand-maintained flag that could drift from the command.
        """
        arguments = set(self.resolved_benchmark_command)
        return {"--telemetry-output", "--trace-graph-json"} <= arguments

    @property
    def modal_entrypoint(self) -> str | None:
        """Return the task's project-relative Modal deployment file, if declared."""
        environment = self.manifest.environment
        if environment is None or environment.modal is None:
            return None
        return environment.modal.entrypoint

    @property
    def workspace_sources(self) -> tuple[WorkspaceSource, ...]:
        """Return the pinned workspace sources declared by the manifest."""
        if self.manifest.workspace is None:
            return ()
        return self.manifest.workspace.sources


def load_input_bundle(path: Path) -> InputBundle:
    """Load and validate a command-based input bundle.

    Evaluators are resolved exactly once relative to the manifest directory.
    They may be siblings of the bundle when the author uses ``..`` components,
    which lets a collection share large inputs without tying resolution to a
    VibeSys source checkout.
    """
    root = path.expanduser().resolve()
    return _load_input_bundle(project_root=root, task_root=root, task_name=None)


def load_project_task(project: Project, task: TaskDirectory) -> InputBundle:
    """Load one repository-native task against its candidate project root."""
    return _load_input_bundle(
        project_root=project.root,
        task_root=task.path,
        task_name=str(task.name),
        task_directory=task,
    )


def _load_input_bundle(
    *,
    project_root: Path,
    task_root: Path,
    task_name: str | None,
    task_directory: TaskDirectory | None = None,
) -> InputBundle:
    """Load a manifest and resolve its commands for project-root execution."""
    root = project_root.expanduser().resolve()
    bundle_root = task_root.expanduser().resolve()
    _validate_project_root(root)
    manifest_path, objective_path = _input_bundle_paths(bundle_root, task_directory)
    manifest = _read_input_manifest(manifest_path, objective_path)
    _validate_modal_entrypoint(root, manifest)
    evaluator_package, resolved_commands = _resolve_evaluator_commands(root, manifest)
    reference_path = _resolve_reference_path(bundle_root, task_directory)
    evaluator_path = _resolve_evaluator_source_path(bundle_root, task_directory, manifest)

    return InputBundle(
        root=root,
        task_root=bundle_root,
        task_name=task_name,
        manifest_path=manifest_path,
        objective_path=objective_path,
        dockerfile_path=(task_directory.dockerfile_path if task_directory is not None else None),
        reference_path=reference_path,
        evaluator_path=evaluator_path,
        evaluator_package_digest=(
            evaluator_package.digest if evaluator_package is not None else None
        ),
        evaluator_package_root=(evaluator_package.root if evaluator_package is not None else None),
        resolved_accuracy_command=resolved_commands[0],
        resolved_benchmark_command=resolved_commands[1],
        manifest=manifest,
    )


def _validate_project_root(root: Path) -> None:
    if not root.exists():
        message = f"--input path does not exist: {root}"
        raise FileNotFoundError(message)
    if not root.is_dir():
        message = f"--input path is not a directory: {root}"
        raise ValueError(message)


def _input_bundle_paths(
    bundle_root: Path,
    task_directory: TaskDirectory | None,
) -> tuple[Path, Path]:
    manifest_path = (
        task_directory.manifest_path if task_directory is not None else bundle_root / MANIFEST_NAME
    )
    objective_path = (
        task_directory.objective_path
        if task_directory is not None
        else bundle_root / "OBJECTIVE.md"
    )
    return manifest_path, objective_path


def _read_input_manifest(manifest_path: Path, objective_path: Path) -> InputManifest:
    if not manifest_path.is_file():
        message = f"Input manifest not found: {manifest_path}"
        raise FileNotFoundError(message)
    if not objective_path.is_file():
        message = f"OBJECTIVE.md not found: {objective_path}"
        raise FileNotFoundError(message)
    try:
        return InputManifest.model_validate(tomllib.loads(manifest_path.read_text()))
    except ValidationError as exc:
        message = f"Invalid input manifest {manifest_path}: {exc}"
        raise ValueError(message) from exc


def _validate_modal_entrypoint(root: Path, manifest: InputManifest) -> None:
    environment = manifest.environment
    modal = environment.modal if environment is not None else None
    if modal is None:
        return
    modal_entrypoint = (root / modal.entrypoint).resolve()
    try:
        modal_entrypoint.relative_to(root)
    except ValueError as exc:
        message = f"environment.modal.entrypoint escapes the project: {modal.entrypoint}"
        raise ValueError(message) from exc
    if not modal_entrypoint.exists():
        message = f"environment.modal.entrypoint does not exist: {modal_entrypoint}"
        raise FileNotFoundError(message)
    if not modal_entrypoint.is_file():
        message = f"environment.modal.entrypoint is not a file: {modal_entrypoint}"
        raise ValueError(message)


def _resolve_evaluator_commands(
    root: Path,
    manifest: InputManifest,
) -> tuple[ResolvedEvaluatorPackage | None, list[tuple[str, ...]]]:
    requirement = manifest.evaluator.package_requirement if manifest.evaluator is not None else None
    evaluator_package = resolve_evaluator_package(requirement) if requirement is not None else None
    commands = [
        _resolve_evaluator_command(root, label, command, evaluator_package)
        for label, command in (
            ("accuracy.command", manifest.accuracy),
            ("benchmark.command", manifest.benchmark),
        )
    ]
    return evaluator_package, commands


def _resolve_evaluator_command(
    root: Path,
    label: str,
    command_spec: InputCommand,
    evaluator_package: ResolvedEvaluatorPackage | None,
) -> tuple[str, ...]:
    if command_spec.entrypoint is not None:
        package = cast("ResolvedEvaluatorPackage", evaluator_package)
        return package.command(command_spec.entrypoint, *command_spec.args)

    command = cast("tuple[str, ...]", command_spec.command)
    executable = Path(command[0])
    if executable.is_absolute():
        message = f"{label} executable must be relative to the project: {command[0]}"
        raise ValueError(message)
    if "/" not in command[0]:
        return command
    resolved = (root / executable).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        message = f"{label} executable escapes the project: {command[0]}"
        raise ValueError(message) from exc
    if not resolved.exists():
        message = f"{label} executable does not exist: {resolved}"
        raise FileNotFoundError(message)
    if not resolved.is_file():
        message = f"{label} executable is not a file: {resolved}"
        raise ValueError(message)
    return command


def _resolve_reference_path(
    bundle_root: Path,
    task_directory: TaskDirectory | None,
) -> Path | None:
    reference_path = bundle_root / "reference"
    if reference_path.exists() or reference_path.is_symlink():
        reference_path = (
            task_directory.resolve("reference") if task_directory is not None else reference_path
        )
    if reference_path.exists() and not reference_path.is_dir():
        message = f"reference path is not a directory: {reference_path}"
        raise ValueError(message)
    return reference_path if reference_path.exists() else None


def _resolve_evaluator_source_path(
    bundle_root: Path,
    task_directory: TaskDirectory | None,
    manifest: InputManifest,
) -> Path | None:
    evaluator = manifest.evaluator
    if evaluator is None or evaluator.source is None:
        return None
    evaluator_path = (
        task_directory.resolve(evaluator.source)
        if task_directory is not None
        else (bundle_root / evaluator.source).resolve()
    )
    if not evaluator_path.exists():
        message = f"evaluator.source path does not exist: {evaluator_path}"
        raise FileNotFoundError(message)
    if not evaluator_path.is_dir():
        message = f"evaluator.source path is not a directory: {evaluator_path}"
        raise ValueError(message)
    return evaluator_path
