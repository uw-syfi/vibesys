"""Provision a self-contained VibeSys project from an input directory."""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from vibesys.evaluators.input_manifest import (
    MANIFEST_NAME,
    EvaluatorInput,
    InputManifest,
    WorkspaceSource,
    render_input_manifest,
)
from vibesys.run.workspace import CopySpec, GitSourceSpec, InputProjectSpec, Workspace
from vs_project.api import Project, is_project_state_path

_PRIVATE_PROJECT_ENTRY_NAMES = frozenset({".git", "agent.toml"})


class ProjectProvisioningError(ValueError):
    """Raised when an input directory cannot be provisioned as a project."""


@dataclass(frozen=True)
class ProjectProvisioningSpec:
    """Materialization dependencies for one copied project.

    ``workspace`` owns the copy and source-materialization mechanisms and must
    be rooted at the requested destination. The other paths are resolved input
    dependencies, normally taken from :class:`~vibesys.evaluators.input_manifest.InputBundle`.
    """

    workspace: Workspace
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
    _validate_destination(source, destination, workspace=spec.workspace)
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
        spec.workspace.create()
        spec.workspace.setup(primary_steps, existing=False)
        evaluator_relative = _materialize_evaluator(
            source,
            destination,
            manifest,
            spec,
        )
        _remove_private_entries(destination)
        if not repository_task:
            normalized = manifest.model_copy(
                update={
                    "workspace": None,
                    "evaluator": (
                        EvaluatorInput(source=evaluator_relative)
                        if evaluator_relative is not None
                        else None
                    ),
                }
            )
            (destination / MANIFEST_NAME).write_text(render_input_manifest(normalized))
    except BaseException as exc:
        try:
            _remove_path(destination)
        except OSError as cleanup_error:
            exc.add_note(
                f"Failed to remove partial provisioned project {destination}: {cleanup_error}"
            )
        raise

    return destination


def _require_input_root(path: Path, *, require_legacy_objective: bool) -> Path:
    root = path.expanduser().resolve()
    if not root.exists():
        raise ProjectProvisioningError(f"input project does not exist: {root}")  # noqa: TRY003
    if not root.is_dir():
        raise ProjectProvisioningError(f"input project is not a directory: {root}")  # noqa: TRY003
    if require_legacy_objective and not (root / "OBJECTIVE.md").is_file():
        raise ProjectProvisioningError(  # noqa: TRY003
            f"OBJECTIVE.md not found: {root / 'OBJECTIVE.md'}"
        )
    return root


def _validate_destination(source: Path, destination: Path, *, workspace: Workspace) -> None:
    if destination == source or destination.is_relative_to(source):
        raise ProjectProvisioningError(  # noqa: TRY003
            f"project destination must be outside the input project: {destination}"
        )
    if destination.exists() or destination.is_symlink():
        raise ProjectProvisioningError(  # noqa: TRY003
            f"project destination already exists: {destination}"
        )
    if workspace.root.expanduser().resolve() != destination:
        raise ProjectProvisioningError(  # noqa: TRY003
            "project workspace root does not match destination: "
            f"{workspace.root.expanduser().resolve()} != {destination}"
        )


def _load_manifest(source: Path) -> InputManifest:
    path = source / MANIFEST_NAME
    if not path.is_file():
        raise ProjectProvisioningError(f"input manifest not found: {path}")  # noqa: TRY003
    try:
        return InputManifest.model_validate(tomllib.loads(path.read_text()))
    except (tomllib.TOMLDecodeError, ValidationError) as exc:
        raise ProjectProvisioningError(  # noqa: TRY003
            f"invalid input manifest {path}: {exc}"
        ) from exc


def _validate_materialization_contract(
    manifest: InputManifest,
    spec: ProjectProvisioningSpec,
) -> None:
    declared_sources = manifest.workspace.sources if manifest.workspace is not None else ()
    if declared_sources != spec.workspace_sources:
        raise ProjectProvisioningError(  # noqa: TRY003
            "workspace source declarations and resolved provisioning sources do not match"
        )
    if any(not source.strip_git for source in spec.workspace_sources):
        raise ProjectProvisioningError(  # noqa: TRY003
            "copied projects require workspace sources with strip_git = true"
        )
    declared_evaluator = manifest.evaluator is not None and manifest.evaluator.source is not None
    if declared_evaluator != (spec.evaluator_source is not None):
        raise ProjectProvisioningError(  # noqa: TRY003
            "evaluator declaration and resolved provisioning source do not match"
        )


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
) -> tuple[CopySpec | GitSourceSpec | InputProjectSpec, ...]:
    steps: list[CopySpec | GitSourceSpec | InputProjectSpec] = []
    steps.extend(GitSourceSpec(source=item) for item in spec.workspace_sources)
    steps.append(
        CopySpec(
            src=source,
            dest=destination,
            reject_collisions=bool(spec.workspace_sources),
            extra_excludes=copy_excludes | spec.input_excludes,
        )
    )
    if spec.input_project_dir is not None:
        steps.append(InputProjectSpec(project_dir=spec.input_project_dir))
    return tuple(steps)


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
        raise ProjectProvisioningError(  # noqa: TRY003
            f"evaluator source is not a directory: {evaluator_source}"
        )
    relative = Path("_evaluator") / evaluator_source.name
    evaluator_destination = destination / relative

    try:
        source_relative = evaluator_source.relative_to(source)
    except ValueError:
        source_relative = None

    if source_relative == relative:
        return relative.as_posix()

    if source_relative is not None:
        _remove_path(destination / source_relative)

    spec.workspace.setup(
        (
            CopySpec(
                src=evaluator_source,
                dest=evaluator_destination,
                respect_gitignore=_is_git_worktree(evaluator_source),
                extra_excludes=_project_copy_excludes(evaluator_source),
                require_absent=evaluator_destination,
                require_absent_message=(
                    "evaluator destination already exists in provisioned project: "
                    f"{relative.as_posix()}"
                ),
            ),
        ),
        existing=True,
    )
    return relative.as_posix()


def _remove_private_entries(root: Path) -> None:
    private_paths = sorted(
        (
            path
            for path in root.rglob("*")
            if not _should_copy_project_entry(path.relative_to(root))
        ),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for path in private_paths:
        _remove_path(path)


def _should_copy_project_entry(relative_path: Path) -> bool:
    """Apply application privacy rules without interpreting state layout."""
    return not is_project_state_path(relative_path) and not any(
        part in _PRIVATE_PROJECT_ENTRY_NAMES or part == ".env" or part.startswith(".env.")
        for part in relative_path.parts
    )


def _is_git_worktree(path: Path) -> bool:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()
