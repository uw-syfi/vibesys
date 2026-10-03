"""Provision a self-contained VibeSys project from an input directory."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import TYPE_CHECKING, Self

from pydantic import ValidationError

from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.inputs import (
    MANIFEST_NAME,
    EvaluatorInput,
    InputManifest,
    WorkspaceSource,
    render_input_manifest,
)
from vibesys.run.workspace_policy import materialization_source
from vs_project.api import (
    OrchestrationDescriptor,
    OrchestrationRunManifest,
    Project,
    RunExecutionRecord,
    is_project_state_path,
)
from vs_runtime.api import OrchestrationResumeDecision
from vs_runtime.api.infrastructure import (
    FreshProjectError,
    FreshProjectErrorKind,
    InputProjectMaterialization,
    ProjectMaterializationStep,
    ProjectMaterializer,
    ProjectRunBaselineMissingError,
    ProjectRunDirtyResumeError,
    ProjectRunMismatchError,
    ProjectRunMismatchKind,
    ProjectTreeCopy,
    RunEnvironmentSpec,
    run_environment_record,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

_PRIVATE_PROJECT_ENTRY_NAMES = frozenset({".git", "agent.toml"})


def installed_vibesys_version() -> str:
    """Return the installed distribution version for portable run metadata."""
    try:
        return distribution_version("vibesys")
    except PackageNotFoundError:
        return "0+unknown"


def validate_agent_role_catalog(
    recorded: Mapping[str, object],
    selected: Mapping[str, object],
) -> None:
    """Reject persisted role IDs that differ from the selected plugin."""
    missing_roles = sorted(selected.keys() - recorded.keys())
    unknown_roles = sorted(recorded.keys() - selected.keys())
    if not missing_roles and not unknown_roles:
        return
    details = []
    if missing_roles:
        details.append(f"missing recorded roles: {', '.join(missing_roles)}")
    if unknown_roles:
        details.append(f"unknown recorded roles: {', '.join(unknown_roles)}")
    raise ConfigurationError(
        ConfigurationDiagnostic(
            code="project_resume_configuration_invalid",
            stage="resume_resolution",
            message=(
                "recorded agent roles do not match the selected orchestration: "
                + "; ".join(details)
            ),
        )
    )


def exact_resume_descriptor(
    recorded: OrchestrationDescriptor,
    requested: OrchestrationDescriptor,
) -> OrchestrationResumeDecision:
    """Require an unchanged orchestration descriptor when no plugin policy exists."""
    if recorded != requested:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=f"resuming orchestration {recorded.id!r} cannot change its descriptor",
            )
        )
    return OrchestrationResumeDecision(descriptor=None)


def resume_orchestration_decision(
    recorded: OrchestrationRunManifest,
    requested: OrchestrationDescriptor,
    environment: RunEnvironmentSpec,
    execution: RunExecutionRecord,
    resume_policy: Callable[
        [OrchestrationDescriptor, OrchestrationDescriptor], OrchestrationResumeDecision
    ],
) -> OrchestrationResumeDecision:
    """Validate persisted run identity before invoking plugin resume policy."""
    if recorded.run_environment != run_environment_record(environment):
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message="resuming a run cannot change its recorded run_environment",
            )
        )
    validate_agent_role_catalog(recorded.execution.agent_roles, execution.agent_roles)
    if recorded.execution != execution:
        changed = ", ".join(
            name
            for name in RunExecutionRecord.model_fields
            if getattr(recorded.execution, name) != getattr(execution, name)
        )
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=f"resuming a run cannot change its recorded execution fields: {changed}",
            )
        )
    if (recorded.orchestration.id, recorded.orchestration.config_version) != (
        requested.id,
        requested.config_version,
    ):
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="project_resume_configuration_mismatch",
                stage="resume_resolution",
                message=(
                    f"run uses orchestration {recorded.orchestration.id!r} version "
                    f"{recorded.orchestration.config_version}, not {requested.id!r} "
                    f"version {requested.config_version}"
                ),
            )
        )
    return resume_policy(recorded.orchestration, requested)


def project_run_configuration_error(
    error: ProjectRunBaselineMissingError | ProjectRunMismatchError | ProjectRunDirtyResumeError,
) -> ConfigurationError:
    """Translate runtime project failures into stable product diagnostics."""
    if isinstance(error, ProjectRunBaselineMissingError):
        code, stage = "project_trusted_baseline_missing", "workspace_setup"
    elif isinstance(error, ProjectRunDirtyResumeError):
        code, stage = "project_resume_configuration_dirty", "resume_resolution"
    else:
        code = {
            ProjectRunMismatchKind.TRUSTED_INPUT_BASELINE: "project_trusted_baseline_mismatch",
            ProjectRunMismatchKind.BRANCH: "project_state_mismatch",
            ProjectRunMismatchKind.TASK: "project_task_mismatch",
        }[error.kind]
        stage = "resume_resolution"
    return ConfigurationError(ConfigurationDiagnostic(code=code, stage=stage, message=str(error)))


class ProjectProvisioningError(ValueError):
    """Raised when an input directory cannot be provisioned as a project."""

    @classmethod
    def input_missing(cls, path: Path) -> Self:
        """Describe a missing input project directory."""
        return cls(f"input project does not exist: {path}")

    @classmethod
    def input_not_directory(cls, path: Path) -> Self:
        """Describe an input path that is not a directory."""
        return cls(f"input project is not a directory: {path}")

    @classmethod
    def objective_missing(cls, path: Path) -> Self:
        """Describe a legacy input project without its objective file."""
        return cls(f"OBJECTIVE.md not found: {path}")

    @classmethod
    def destination_inside_input(cls, path: Path) -> Self:
        """Describe a destination that would copy a project into itself."""
        return cls(f"project destination must be outside the input project: {path}")

    @classmethod
    def destination_exists(cls, path: Path) -> Self:
        """Describe a destination that is already present."""
        return cls(f"project destination already exists: {path}")

    @classmethod
    def workspace_root_mismatch(cls, workspace_root: Path, destination: Path) -> Self:
        """Describe a workspace whose root differs from the destination."""
        return cls(
            f"project workspace root does not match destination: {workspace_root} != {destination}"
        )

    @classmethod
    def manifest_missing(cls, path: Path) -> Self:
        """Describe an input directory without its manifest."""
        return cls(f"input manifest not found: {path}")

    @classmethod
    def invalid_manifest(cls, path: Path, error: Exception) -> Self:
        """Describe a manifest that cannot be parsed or validated."""
        return cls(f"invalid input manifest {path}: {error}")

    @classmethod
    def workspace_sources_mismatch(cls) -> Self:
        """Describe mismatched declared and resolved workspace sources."""
        return cls("workspace source declarations and resolved provisioning sources do not match")

    @classmethod
    def workspace_sources_require_git_strip(cls) -> Self:
        """Describe workspace sources that retain repository metadata."""
        return cls("copied projects require workspace sources with strip_git = true")

    @classmethod
    def evaluator_declaration_mismatch(cls) -> Self:
        """Describe mismatched evaluator declarations and resolved sources."""
        return cls("evaluator declaration and resolved provisioning source do not match")

    @classmethod
    def evaluator_source_not_directory(cls, path: Path) -> Self:
        """Describe a resolved evaluator source that is not a directory."""
        return cls(f"evaluator source is not a directory: {path}")


@dataclass(frozen=True)
class ProjectProvisioningSpec:
    """Materialization dependencies for one copied project.

    ``materializer`` owns copy and source-materialization mechanics and must be
    rooted at the requested destination. The other paths are resolved input
    dependencies, normally taken from :class:`~vibesys.inputs.InputBundle`.
    """

    materializer: ProjectMaterializer
    workspace_sources: tuple[WorkspaceSource, ...] = ()
    evaluator_source: Path | None = None
    task_name: str | None = None
    input_project_dir: Path | None = None
    input_excludes: frozenset[str] = frozenset()


def provision_project(
    input_root: Path,
    destination_root: Path,
    *,
    spec: ProjectProvisioningSpec,
) -> Path:
    """Copy and normalize ``input_root`` into one self-contained project root.

    The destination must not exist and must be outside the input tree. The
    The function does not initialize Git or VibeSys project metadata. If any
    copy, source checkout, or manifest rewrite fails, the newly created
    destination is removed.
    """
    source = _require_input_root(
        input_root,
        require_legacy_objective=spec.task_name is None,
    )
    destination = destination_root.expanduser().resolve()
    repository_task = spec.task_name is not None
    manifest_root = (
        Project.open(source).select_task(spec.task_name).path if repository_task else source
    )
    manifest = _load_manifest(manifest_root)
    _validate_materialization_contract(manifest, spec)

    copy_excludes = _project_copy_excludes(
        source,
        preserve_configuration=repository_task,
    )
    primary_steps = _primary_steps(source, destination, spec, copy_excludes)

    try:
        with spec.materializer.fresh_project(source, destination):
            spec.materializer.materialize(primary_steps, existing=False)
            evaluator_relative = _materialize_evaluator(
                source,
                destination,
                manifest,
                spec,
            )
            _remove_private_entries(destination, materializer=spec.materializer)
            if not repository_task:
                normalized = manifest.model_copy(
                    update={
                        "workspace": None,
                        "evaluator": _provisioned_evaluator(manifest, evaluator_relative),
                    }
                )
                (destination / MANIFEST_NAME).write_text(render_input_manifest(normalized))
    except FreshProjectError as exc:
        raise _provisioning_error(exc) from exc

    return destination


def _require_input_root(path: Path, *, require_legacy_objective: bool) -> Path:
    root = path.expanduser().resolve()
    if not root.exists():
        raise ProjectProvisioningError.input_missing(root)
    if not root.is_dir():
        raise ProjectProvisioningError.input_not_directory(root)
    if require_legacy_objective and not (root / "OBJECTIVE.md").is_file():
        raise ProjectProvisioningError.objective_missing(root / "OBJECTIVE.md")
    return root


def _provisioning_error(error: FreshProjectError) -> ProjectProvisioningError:
    if error.kind is FreshProjectErrorKind.DESTINATION_INSIDE_SOURCE:
        return ProjectProvisioningError.destination_inside_input(error.destination)
    if error.kind is FreshProjectErrorKind.DESTINATION_EXISTS:
        return ProjectProvisioningError.destination_exists(error.destination)
    return ProjectProvisioningError.workspace_root_mismatch(
        error.materializer_root,
        error.destination,
    )


def _load_manifest(source: Path) -> InputManifest:
    path = source / MANIFEST_NAME
    if not path.is_file():
        raise ProjectProvisioningError.manifest_missing(path)
    try:
        return InputManifest.model_validate(tomllib.loads(path.read_text()))
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ProjectProvisioningError.invalid_manifest(path, exc) from exc


def _validate_materialization_contract(
    manifest: InputManifest,
    spec: ProjectProvisioningSpec,
) -> None:
    declared_sources = manifest.workspace.sources if manifest.workspace is not None else ()
    if declared_sources != spec.workspace_sources:
        raise ProjectProvisioningError.workspace_sources_mismatch()
    if any(not source.strip_git for source in spec.workspace_sources):
        raise ProjectProvisioningError.workspace_sources_require_git_strip()
    declared_evaluator = manifest.evaluator is not None and manifest.evaluator.source is not None
    if declared_evaluator != (spec.evaluator_source is not None):
        raise ProjectProvisioningError.evaluator_declaration_mismatch()


def _project_copy_excludes(
    source: Path,
    *,
    preserve_configuration: bool = False,
) -> frozenset[str]:
    excluded = {
        child.name for child in source.iterdir() if not _should_copy_project_entry(Path(child.name))
    }
    if not preserve_configuration:
        configuration = Project.open(source).state.sandbox_paths().read_only_path
        if configuration is not None and len(configuration.parts) == 1:
            excluded.add(configuration.name)
    return frozenset(excluded)


def _primary_steps(
    source: Path,
    destination: Path,
    spec: ProjectProvisioningSpec,
    copy_excludes: frozenset[str],
) -> tuple[ProjectMaterializationStep, ...]:
    steps: list[ProjectMaterializationStep] = []
    steps.extend(materialization_source(item) for item in spec.workspace_sources)
    steps.append(
        ProjectTreeCopy(
            src=source,
            dest=destination,
            reject_collisions=bool(spec.workspace_sources),
            extra_excludes=copy_excludes | spec.input_excludes,
        )
    )
    if spec.input_project_dir is not None:
        steps.append(InputProjectMaterialization(project_dir=spec.input_project_dir))
    return tuple(steps)


def _provisioned_evaluator(
    manifest: InputManifest, evaluator_relative: str | None
) -> EvaluatorInput | None:
    """Return the evaluator declaration the provisioned project's manifest carries.

    A copied evaluator source is relocated into the project. A packaged evaluator
    is resolved by name and version, not copied, so its declaration stays as is;
    dropping it would leave entrypoint commands without their package.
    """
    if evaluator_relative is not None:
        return EvaluatorInput(source=evaluator_relative)
    if manifest.evaluator is not None and manifest.evaluator.package_requirement is not None:
        return manifest.evaluator
    return None


def _materialize_evaluator(
    source: Path,
    destination: Path,
    manifest: InputManifest,
    spec: ProjectProvisioningSpec,
) -> str | None:
    if manifest.evaluator is None or spec.evaluator_source is None:
        return None

    evaluator_source = spec.evaluator_source.expanduser().resolve()
    if not evaluator_source.is_dir():
        raise ProjectProvisioningError.evaluator_source_not_directory(evaluator_source)
    relative = Path("_evaluator") / evaluator_source.name
    evaluator_destination = destination / relative

    spec.materializer.relocate_copied_tree(
        ProjectTreeCopy(
            src=evaluator_source,
            dest=evaluator_destination,
            extra_excludes=_project_copy_excludes(evaluator_source),
            require_absent=evaluator_destination,
            require_absent_message=(
                "evaluator destination already exists in provisioned project: "
                f"{relative.as_posix()}"
            ),
        ),
        copied_from=source,
    )
    return relative.as_posix()


def _remove_private_entries(root: Path, *, materializer: ProjectMaterializer) -> None:
    private_paths = sorted(
        (
            path
            for path in root.rglob("*")
            if not _should_copy_project_entry(path.relative_to(root))
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    materializer.remove_paths(private_paths)


def _should_copy_project_entry(relative_path: Path) -> bool:
    """Apply application privacy rules without interpreting state layout."""
    return not is_project_state_path(relative_path) and not any(
        part in _PRIVATE_PROJECT_ENTRY_NAMES or part == ".env" or part.startswith(".env.")
        for part in relative_path.parts
    )
