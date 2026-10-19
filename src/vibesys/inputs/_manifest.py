"""Command-based target input manifests."""

from __future__ import annotations

import json
import shlex
import tomllib
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast
from urllib.parse import urlparse

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from vibesys.config import BUNDLED_RESOURCES
from vibesys.constants import ComputeBackend, DomainName
from vibesys.inputs._plan import TaskPlan, check_plan_documents, plan_toml_lines
from vs_project.api import CatalogueInput, Project, RunResourceRequest
from vs_runtime.api.infrastructure import (
    EvaluatorPackageNotFoundError,
    EvaluatorPackageRequirement,
    check_unread_paths,
    resolve_evaluator_package,
)

if TYPE_CHECKING:
    from vs_project.api import TaskDirectory
    from vs_runtime.api.infrastructure import ResolvedEvaluatorPackage

MANIFEST_NAME = "vibesys.input.toml"
_MIN_COMMIT_HASH_LENGTH = 7
_MAX_COMMIT_HASH_LENGTH = 64
PROTOCOL_OUTPUT_FLAG = "--vs-output"
_MISSING_EVALUATOR_RESOURCES_MESSAGE = (
    "VibeSys evaluator package resources are not available; install a complete "
    "VibeSys distribution or pass packages_root"
)


def benchmark_output_argument(
    result: BenchmarkResult | None, result_protocol: Literal[2] | None
) -> str | None:
    """Return the evaluator-owned output flag for a declared benchmark contract."""
    if result is not None:
        return result.json_argument
    if result_protocol is not None:
        return PROTOCOL_OUTPUT_FLAG
    return None


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
    unread: tuple[str, ...] = ()
    """Workspace paths (files or directories) the evaluation never reads, such as agent notes.

    A commit that changes only these is the same measured input as one an earlier run
    already measured, so the runs of both are samples of one measurement and the official
    measurement can reuse them instead of running again (see ``docs/running-vibesys.md``).
    """

    _check_unread = field_validator("unread")(check_unread_paths)


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
    """A task-local evaluator source tree or an exact reusable evaluator package."""

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
            message = "source must be relative to the task directory"
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
        """Return the exact package requirement, if this is not a source tree."""
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
    #: The task's trust rule: a benchmark counts as evidence only if its accuracy stage
    #: passed. A task whose benchmark is meaningful without a passing accuracy check
    #: declares ``requires_accuracy = false``.
    requires_accuracy: StrictBool = True

    @model_validator(mode="after")
    def _one_result_contract(self) -> BenchmarkCommand:
        if self.result is not None and self.result_protocol is not None:
            message = (
                "benchmark.result and benchmark.result_protocol are mutually exclusive; "
                "declare only one"
            )
            raise ValueError(message)
        return self


class BuildCommand(BaseModel):
    """The candidate's own build step: argv run from the root of the measured checkout.

    The build is candidate code. The framework runs it confined, in a fresh checkout of the
    measured commit, before the trusted check and measure stages, and then verifies the
    outputs the evaluator package requires (``candidate_outputs``). It is never an evaluator
    entrypoint: an entrypoint is trusted evaluator code, a build is not.
    """

    model_config = ConfigDict(extra="forbid")

    command: tuple[str, ...]
    timeout_seconds: int | None = Field(default=None, gt=0)

    @field_validator("command")
    @classmethod
    def _non_empty_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            message = "command must contain at least one argv element"
            raise ValueError(message)
        if any(not part for part in value):
            message = "command elements must be non-empty strings"
            raise ValueError(message)
        if Path(value[0]).is_absolute():
            message = "command executable must be a program name or a project-relative path"
            raise ValueError(message)
        return value


class AgentInput(BaseModel):
    """Agent-loop metadata declared by an input bundle."""

    model_config = ConfigDict(extra="forbid")

    domain: DomainName
    interface: Literal["inprocess", "service"] | None = None
    """How the evaluator reaches the candidate; the ``--interface`` flag overrides it, and
    ``inprocess`` is the default when neither says."""


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
    #: The task's candidate is a container topology (``docker compose``, kind):
    #: it must run in the Docker environment, whose sandbox container gets a
    #: Docker daemon of its own under Sysbox. Selecting that environment is
    #: automatic, like a task-owned Dockerfile.
    docker_in_docker: StrictBool = False


class StageKind(StrEnum):
    """A kind of stage a task's measurement can run.

    The registry of executable kinds. A stage is an ordered, recorded operation named by its
    kind and labelled for people; a new kind (an evaluation SDK's, say) is a new member here
    and an executor that runs it, not a new fixed slot in the task model.
    """

    BUILD = "build"
    CHECK = "check"
    MEASURE = "measure"


class StageDeclaration(BaseModel):
    """One stage of a task's measurement as the task declares it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: StageKind
    label: str = Field(min_length=1)
    timeout_seconds: int | None = Field(default=None, gt=0)


_SHORT_FORM = {"check": "accuracy", "measure": "benchmark"}
"""Each short-form stage key and the existing-form key it compiles to."""


class InputManifest(BaseModel):
    """Versioned evaluator-command manifest for an input bundle.

    A task declares its evaluation in one of two forms that compile to the same model: the
    short form (``build``, ``check``, ``measure``, ``metric``) or the existing form
    (``[accuracy]``, ``[benchmark]``, optionally ``[build]``). ``check`` is the accuracy
    stage and ``measure`` the benchmark stage; a short-form ``measure`` speaks evaluator
    result protocol 2 unless it declares a ``result`` contract. ``stages`` is the compiled,
    ordered list of what one measurement runs.
    """

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    backends: tuple[ComputeBackend, ...] = ()
    """The compute backends the task targets, in preference order; the run takes its backend from
    them (one it can run on the host, when several). Empty means undeclared: the run falls back
    to ``[backend].name`` in ``agent.toml`` (default ``cuda``), or to ``[resources]``'s
    ``accelerator_backend`` when the task declares those resources."""
    agent: AgentInput
    profile_guided: ProfileGuidedInput | None = None
    build: BuildCommand | None = None
    """The candidate's confined build, run once per measured commit before check and measure."""
    accuracy: InputCommand = Field(validation_alias=AliasChoices("accuracy", "check"))
    benchmark: BenchmarkCommand = Field(validation_alias=AliasChoices("benchmark", "measure"))
    metric: str | None = Field(default=None, min_length=1)
    """The headline metric the measure stage reports."""
    profile: InputCommand | None = None
    resources: RunResourceRequest | None = None
    environment: EnvironmentInput | None = None
    workspace: WorkspaceInput | None = None
    evaluator: EvaluatorInput | None = None
    plan: TaskPlan | None = None
    """The design a plan-mode run follows; inert otherwise."""
    catalogue: CatalogueInput | None = None
    """What an example task is about, for the catalogue a client offers; inert for a run."""

    @model_validator(mode="before")
    @classmethod
    def _compile_short_form(cls, data: object) -> object:
        """Refuse a stage declared in both forms; a short-form measure defaults to protocol 2."""
        if not isinstance(data, dict):
            return data
        mixed = [
            f"{short} and {existing}"
            for short, existing in _SHORT_FORM.items()
            if short in data and existing in data
        ]
        if mixed:
            message = (
                f"declare each stage once: {', '.join(mixed)} name the same stage; use the "
                "short form (build, check, measure, metric) or the existing form "
                "([accuracy], [benchmark]), not both"
            )
            raise ValueError(message)
        measure = data.get("measure")
        if isinstance(measure, dict) and "result" not in measure:
            return {**data, "measure": {"result_protocol": 2, **measure}}
        return data

    @model_validator(mode="after")
    def _backends_agree(self) -> InputManifest:
        """``backends`` names each backend once and contains ``[resources]``'s backend."""
        repeated = sorted({b for b in self.backends if self.backends.count(b) > 1})
        if repeated:
            message = f"backends names each backend once; repeated: {', '.join(repeated)}"
            raise ValueError(message)
        resources = self.resources
        if (
            self.backends
            and resources is not None
            and ComputeBackend(resources.accelerator_backend) not in self.backends
        ):
            message = (
                f"resources.accelerator_backend {resources.accelerator_backend!r} is not one of "
                f"backends ({', '.join(self.backends)}); declare it there or change the resources"
            )
            raise ValueError(message)
        return self

    @property
    def declared_backends(self) -> tuple[ComputeBackend, ...]:
        """The backends the task targets: ``backends``, else its ``[resources]``' one, else none."""
        if self.backends:
            return self.backends
        if self.resources is not None:
            return (ComputeBackend(self.resources.accelerator_backend),)
        return ()

    @property
    def stages(self) -> tuple[StageDeclaration, ...]:
        """What one measurement of a candidate runs, in order, whichever form declared it."""
        declared: tuple[tuple[StageKind, BuildCommand | InputCommand | None], ...] = (
            (StageKind.BUILD, self.build),
            (StageKind.CHECK, self.accuracy),
            (StageKind.MEASURE, self.benchmark),
        )
        return tuple(
            StageDeclaration(kind=kind, label=kind.value, timeout_seconds=command.timeout_seconds)
            for kind, command in declared
            if command is not None
        )

    @model_validator(mode="after")
    def _validate_cross_references(self) -> InputManifest:
        if (
            self.metric is not None
            and self.benchmark.result is not None
            and self.benchmark.result.metric != self.metric
        ):
            message = (
                f"metric {self.metric!r} disagrees with benchmark.result.metric "
                f"{self.benchmark.result.metric!r}; declare the headline metric once"
            )
            raise ValueError(message)
        uses_entrypoint = any(
            command.entrypoint is not None
            for command in (self.accuracy, self.benchmark, self.profile)
            if command is not None
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
    lines = [f"version = {manifest.version}"]
    if manifest.metric is not None:
        lines.append(f"metric = {_toml_string(manifest.metric)}")
    if manifest.backends:
        lines.append(f"backends = {_toml_array(tuple(str(b) for b in manifest.backends))}")
    lines += [
        "",
        "[agent]",
        f"domain = {_toml_string(manifest.agent.domain.value)}",
    ]
    if manifest.agent.interface is not None:
        lines.append(f"interface = {_toml_string(manifest.agent.interface)}")
    _append_profile_guided(lines, manifest)
    _append_build(lines, manifest)
    _append_accuracy(lines, manifest)
    _append_environment_and_resources(lines, manifest)
    _append_benchmark(lines, manifest)
    _append_profile(lines, manifest)
    _append_workspace_sources(lines, manifest)
    _append_evaluator(lines, manifest)
    if manifest.plan is not None:
        lines += plan_toml_lines(manifest.plan)
    return "\n".join(lines) + "\n"


def _append_profile(lines: list[str], manifest: InputManifest) -> None:
    if manifest.profile is None:
        return
    command = manifest.profile
    lines.extend(["", "[profile]"])
    if command.command is not None:
        lines.append(f"command = {_toml_array(command.command)}")
    else:
        lines.append(
            f"entrypoint = {_toml_string(_required_manifest_text(command.entrypoint, 'profile.entrypoint'))}"
        )
        lines.append(f"args = {_toml_array(command.args)}")
    if command.timeout_seconds is not None:
        lines.append(f"timeout_seconds = {command.timeout_seconds}")


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


def _append_build(lines: list[str], manifest: InputManifest) -> None:
    if manifest.build is None:
        return
    lines.extend(["", "[build]", f"command = {_toml_array(manifest.build.command)}"])
    if manifest.build.timeout_seconds is not None:
        lines.append(f"timeout_seconds = {manifest.build.timeout_seconds}")


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
    if manifest.environment is not None and manifest.environment.docker_in_docker:
        lines.extend(["", "[environment]", "docker_in_docker = true"])
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
    if manifest.benchmark.result_protocol is not None:
        lines.append(f"result_protocol = {manifest.benchmark.result_protocol}")
    if not manifest.benchmark.requires_accuracy:
        lines.append("requires_accuracy = false")
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
    if manifest.workspace is not None and manifest.workspace.unread:
        lines.extend(["", "[workspace]", f"unread = {_toml_array(manifest.workspace.unread)}"])
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

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    root: Path
    task_root: Path
    task_name: str
    manifest_path: Path
    objective_path: Path
    dockerfile_path: Path | None
    reference_path: Path | None
    evaluator_path: Path | None
    evaluator_package_digest: str | None = None
    evaluator_package_root: Path | None = None
    candidate_outputs: tuple[str, ...] = ()
    """The files the evaluator package requires a candidate's build to produce."""
    resolved_accuracy_command: tuple[str, ...]
    resolved_benchmark_command: tuple[str, ...]
    resolved_profile_command: tuple[str, ...] | None = None
    manifest: InputManifest

    @property
    def objective(self) -> str:
        """Read the objective text from its declared file."""
        return self.objective_path.read_text()

    @property
    def missing_build(self) -> str | None:
        """Why the task cannot be measured for want of a build, or None.

        A task whose evaluator requires candidate outputs must declare the ``[build]`` that
        produces them; without one every measurement, the baseline's included, fails.
        """
        if not self.candidate_outputs or self.manifest.build is not None:
            return None
        outputs = ", ".join(self.candidate_outputs)
        manifest = (
            self.manifest_path.relative_to(self.root)
            if self.manifest_path.is_relative_to(self.root)
            else self.manifest_path
        )
        return (
            f"the evaluator requires the candidate build output {outputs}, but the task "
            f"declares no build: add a [build] whose command produces it to {manifest}"
        )

    @property
    def accuracy_command(self) -> tuple[str, ...]:
        """Return the resolved accuracy evaluator argv."""
        return self.resolved_accuracy_command

    @property
    def benchmark_command(self) -> tuple[str, ...]:
        """Return the resolved benchmark evaluator argv."""
        return self.resolved_benchmark_command

    @property
    def profile_command(self) -> tuple[str, ...] | None:
        """Return the trusted diagnostic workload, independent of benchmark gates."""
        return self.resolved_profile_command

    @property
    def declared_backends(self) -> tuple[ComputeBackend, ...]:
        """The compute backends the task targets (see ``InputManifest.declared_backends``)."""
        return self.manifest.declared_backends

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
    def benchmark_output_argument(self) -> str | None:
        """Return the output flag expected by this bundle's trusted benchmark."""
        return benchmark_output_argument(self.benchmark_result, self.benchmark_result_protocol)

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
    def docker_in_docker(self) -> bool:
        """Return whether the task declared that its candidate needs a Docker daemon."""
        environment = self.manifest.environment
        return environment is not None and environment.docker_in_docker

    @property
    def modal_entrypoint(self) -> str | None:
        """Return the task's project-relative Modal deployment file, if declared."""
        environment = self.manifest.environment
        if environment is None or environment.modal is None:
            return None
        return environment.modal.entrypoint

    @property
    def plan(self) -> TaskPlan | None:
        """Return the implementation plan the task declares, if any."""
        return self.manifest.plan

    @property
    def workspace_sources(self) -> tuple[WorkspaceSource, ...]:
        """Return the pinned workspace sources declared by the manifest."""
        if self.manifest.workspace is None:
            return ()
        return self.manifest.workspace.sources


def reject_root_manifest(project_root: Path) -> None:
    """Fail when ``project_root`` carries a root-level ``vibesys.input.toml``.

    Task inputs live in ``.vibesys/tasks/<task>/``; a manifest at the project
    root is the retired legacy layout and would otherwise be ignored silently.
    """
    manifest = project_root / MANIFEST_NAME
    if manifest.is_file():
        message = (
            f"{manifest}: a root-level input manifest is not supported; move it, with "
            f"OBJECTIVE.md and the files its commands read, to "
            f".vibesys/tasks/<task>/{MANIFEST_NAME}"
        )
        raise ValueError(message)


def load_task(project_root: Path, task_name: str | None = None) -> InputBundle:
    """Load the selected task of the project at ``project_root``.

    ``task_name`` may be omitted when the project defines exactly one task.
    Raises ``FileNotFoundError`` or ``ValueError`` (including
    ``vs_project`` layout errors) that name the offending path.
    """
    root = project_root.expanduser().resolve()
    _validate_project_root(root)
    reject_root_manifest(root)
    project = Project.open(root)
    if not project.is_initialized():
        message = (
            f"{root} is not a VibeSys project: {project.configuration_path()}/tasks/ does not exist"
        )
        raise ValueError(message)
    return load_project_task(project, project.select_task(task_name))


def load_project_task(project: Project, task: TaskDirectory) -> InputBundle:
    """Load one repository-native task against its candidate project root."""
    root = project.root.expanduser().resolve()
    _validate_project_root(root)
    reject_root_manifest(root)
    manifest = _read_input_manifest(task.manifest_path, task.objective_path)
    _validate_modal_entrypoint(root, manifest)
    evaluator_package, resolved_commands = _resolve_evaluator_commands(root, manifest)
    reference_path = _resolve_reference_path(task)
    evaluator_path = _resolve_evaluator_source_path(task, manifest)
    if manifest.plan is not None:
        check_plan_documents(task, manifest.plan)

    return InputBundle(
        root=root,
        task_root=task.path,
        task_name=str(task.name),
        manifest_path=task.manifest_path,
        objective_path=task.objective_path,
        dockerfile_path=task.dockerfile_path,
        reference_path=reference_path,
        evaluator_path=evaluator_path,
        evaluator_package_digest=(
            evaluator_package.digest if evaluator_package is not None else None
        ),
        evaluator_package_root=(evaluator_package.root if evaluator_package is not None else None),
        candidate_outputs=(
            evaluator_package.metadata.candidate_outputs if evaluator_package is not None else ()
        ),
        resolved_accuracy_command=resolved_commands[0],
        resolved_benchmark_command=resolved_commands[1],
        resolved_profile_command=(resolved_commands[2] if manifest.profile is not None else None),
        manifest=manifest,
    )


def _validate_project_root(root: Path) -> None:
    if not root.exists():
        message = f"project path does not exist: {root}"
        raise FileNotFoundError(message)
    if not root.is_dir():
        message = f"project path is not a directory: {root}"
        raise ValueError(message)


def _read_input_manifest(manifest_path: Path, objective_path: Path) -> InputManifest:
    if not manifest_path.is_file():
        message = f"Input manifest not found: {manifest_path}"
        raise FileNotFoundError(message)
    if not objective_path.is_file():
        message = f"OBJECTIVE.md not found: {objective_path}"
        raise FileNotFoundError(message)
    return load_input_manifest(manifest_path)


class InputManifestParseError(ValueError):
    """An input manifest that is not TOML or does not match the manifest schema."""

    def __init__(self, path: Path, cause: tomllib.TOMLDecodeError | ValidationError) -> None:
        """Name the manifest and keep the parser's or validator's error."""
        super().__init__(f"Invalid input manifest {path}: {cause}")
        self.path = path
        self.cause = cause


def load_input_manifest(manifest_path: Path) -> InputManifest:
    """Read, parse, and validate one input manifest file (``vibesys.toml``).

    The one reader of the manifest format: a file that is not TOML or does not
    match the schema raises ``InputManifestParseError``; an unreadable file
    raises ``OSError``.
    """
    try:
        return InputManifest.model_validate(
            tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        )
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise InputManifestParseError(manifest_path, exc) from exc


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
    evaluator_package = (
        _resolve_bundled_evaluator_package(requirement) if requirement is not None else None
    )
    commands = [
        _resolve_evaluator_command(root, label, command, evaluator_package)
        for label, command in (
            ("accuracy.command", manifest.accuracy),
            ("benchmark.command", manifest.benchmark),
            ("profile.command", manifest.profile),
        )
        if command is not None
    ]
    return evaluator_package, commands


def _resolve_bundled_evaluator_package(
    requirement: EvaluatorPackageRequirement,
) -> ResolvedEvaluatorPackage:
    """Resolve an authored package requirement from VibeSys's bundled collection."""
    packages_root = BUNDLED_RESOURCES.directory("evaluators")
    if packages_root is None:
        raise EvaluatorPackageNotFoundError(_MISSING_EVALUATOR_RESOURCES_MESSAGE)
    return resolve_evaluator_package(packages_root, requirement)


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


def _resolve_reference_path(task: TaskDirectory) -> Path | None:
    reference_path = task.path / "reference"
    if reference_path.exists() or reference_path.is_symlink():
        reference_path = task.resolve("reference")
    if reference_path.exists() and not reference_path.is_dir():
        message = f"reference path is not a directory: {reference_path}"
        raise ValueError(message)
    return reference_path if reference_path.exists() else None


def _resolve_evaluator_source_path(task: TaskDirectory, manifest: InputManifest) -> Path | None:
    evaluator = manifest.evaluator
    if evaluator is None or evaluator.source is None:
        return None
    evaluator_path = task.resolve(evaluator.source)
    if not evaluator_path.exists():
        message = f"evaluator.source path does not exist: {evaluator_path}"
        raise FileNotFoundError(message)
    if not evaluator_path.is_dir():
        message = f"evaluator.source path is not a directory: {evaluator_path}"
        raise ValueError(message)
    return evaluator_path
